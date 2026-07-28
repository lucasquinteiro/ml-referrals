"""
Real affiliate links, via Mercado Libre's own link builder.

The affiliate dashboard at /afiliados/linkbuilder calls an internal endpoint:

    POST /affiliate-program/api/v2/affiliates/createLink
    {"urls": [...], "tag": "<your tag>"}

It returns both a short link (meli.la/...) and the full one, and the full one
carries a signed `ref=` blob that we cannot construct ourselves. That signature
is why this path is preferred over just appending matt_word/matt_tool: it is
byte-for-byte what the dashboard produces, so attribution behaves identically.

It is not a documented public API — it's the site's own endpoint, called the way
the browser calls it: with your logged-in session (from `./run login`) and the
CSRF token the page carries. No credentials are handled here; the session file
is the same one Playwright saved.

Falls back to affiliate.py's param form whenever this is unavailable, so a
broken session degrades to a working-but-unsigned link rather than no link.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import log_step, log_warn

LINKBUILDER_URL = "/afiliados/linkbuilder"
CREATE_LINK_PATH = "/affiliate-program/api/v2/affiliates/createLink"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Runs inside the page so the request is same-origin and the session cookies
# ride along automatically — no header assembly, no cookie handling here.
_CREATE_JS = """
async ({path, urls, tag}) => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      'accept': 'application/json, text/plain, */*',
      'x-csrf-token': csrf,
    },
    body: JSON.stringify({urls, tag}),
  });
  // Read the stream exactly once — calling .json() then .text() on the same
  // Response throws "body stream already read" and loses the real error.
  const raw = await r.text();
  let body;
  try { body = JSON.parse(raw); } catch (e) { body = {parseError: raw.slice(0, 400)}; }
  return {httpStatus: r.status, body};
}
"""


class AffiliateAPIError(RuntimeError):
    pass


class NotAnAffiliateError(AffiliateAPIError):
    """The logged-in account isn't enrolled in the affiliate program."""


def _walk_strings(value: Any) -> list[str]:
    """Every string anywhere in a nested JSON structure."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _walk_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _walk_strings(v)]
    return []


def _extract(entry: dict[str, Any], source_url: str = "") -> Optional[dict[str, str]]:
    """Pull the short/long pair out of one result entry.

    Identifies them by *shape* rather than by field name — a shortener host for
    the short link, the tracking params for the long one. The field names in
    this response aren't documented anywhere and have no stability guarantee,
    so matching on content survives a rename.
    """
    # The field names as of 2026-07. Checked first because they're exact.
    short = (entry.get("short_url") or "").strip()
    full = (entry.get("long_url") or "").strip()
    if short or full:
        return {"short": short, "full": full or short}

    # Renamed or restructured: fall back to identifying them by content.
    for s in _walk_strings(entry):
        if not s.startswith("http"):
            continue
        if s == source_url:
            continue  # this is the echoed input, not a generated link
        if not short and any(h in s for h in ("meli.la", "/sec/", "mercadolibre.com/sec")):
            short = s
        elif not full and ("matt_word=" in s or "/social/" in s or "ref=" in s):
            full = s

    if not short and not full:
        return None
    return {"short": short, "full": full or short}


class AffiliateLinkBuilder:
    """Batch-generates affiliate links using the saved Mercado Libre session.

    Usage:
        with AffiliateLinkBuilder(site, tag) as b:
            links = b.create([url1, url2])   # {url: {"short":..., "full":...}}
    """

    def __init__(self, site: str, tag: str, *, storage_state: Any = None) -> None:
        self.site = site.rstrip("/")
        self.tag = tag
        self.storage_state = storage_state
        self._session = None
        self._page = None

    def __enter__(self) -> "AffiliateLinkBuilder":
        import auth
        from lib import mlgate

        if not auth.has_session(auth.AFFILIATE):
            raise AffiliateAPIError(
                "No affiliate session. Run `./run login --role affiliate` and "
                "sign in with the account enrolled in the Programa de Afiliados."
            )

        self._session = auth.BrowserSession(auth.AFFILIATE, user_agent=UA)
        ctx = self._session.__enter__()
        self._page = ctx.new_page()
        mlgate.wait(mlgate.AFFILIATE, label="linkbuilder page")
        self._page.goto(
            self.site + LINKBUILDER_URL, wait_until="domcontentloaded", timeout=60_000
        )
        self._page.wait_for_timeout(3000)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._session:
            self._session.__exit__(*exc)

    def list_tags(self) -> list[dict[str, Any]]:
        """The affiliate tags on this account, newest first.

        An account can hold several — typically the auto-generated one named
        after the nickname, plus any you created. Only one is `in_use`. Read
        from the link builder page's own embedded state; there's no tags
        endpoint (the obvious paths 404).
        """
        import json
        import re

        assert self._page is not None, "use as a context manager"
        html = self._page.content()
        m = re.search(r'"tags"\s*:\s*(\[[^\]]*\])', html)
        if not m:
            return []
        try:
            tags = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
        return sorted(
            [t for t in tags if isinstance(t, dict) and t.get("tag")],
            key=lambda t: t.get("generated_date", ""),
            reverse=True,
        )

    def create(self, urls: list[str]) -> dict[str, dict[str, str]]:
        """Generate links for `urls`. Returns {input_url: {"short", "full"}}.

        Missing entries mean that URL failed; the caller falls back to the
        param form for those rather than dropping the product.
        """
        if not urls:
            return {}
        assert self._page is not None, "use as a context manager"

        from lib import mlgate

        mlgate.wait(mlgate.AFFILIATE, label=f"createLink x{len(urls)}")
        res = self._page.evaluate(
            _CREATE_JS, {"path": CREATE_LINK_PATH, "urls": urls, "tag": self.tag}
        )
        body = res.get("body") or {}

        if res.get("httpStatus") != 200:
            raise AffiliateAPIError(
                f"createLink returned HTTP {res.get('httpStatus')}: {str(body)[:300]}"
            )

        entries = body.get("urls") or []
        out = _parse_response(body, urls)

        if entries and not out:
            log_warn(f"createLink returned no usable links: {str(body)[:200]}")
        return out


def _parse_response(body: dict[str, Any], urls: list[str]) -> dict[str, dict[str, str]]:
    """Shared result handling for both transports."""
    out: dict[str, dict[str, str]] = {}
    for i, entry in enumerate(body.get("urls") or []):
        source = (
            entry.get("origin_url")
            or entry.get("entity")
            or (urls[i] if i < len(urls) else "")
        )
        if entry.get("error_code") or entry.get("message"):
            msg = entry.get("message", "")
            if entry.get("error_code") == 109 or "not found affiliate user" in msg:
                raise NotAnAffiliateError(
                    f"Mercado Libre says this account isn't an affiliate ({msg}).\n"
                    "  The saved session is for a different account than the one "
                    "enrolled in the Programa de Afiliados.\n"
                    "  Fix: run `./run login --role affiliate` with the enrolled account."
                )
            log_warn(f"createLink failed for {source[:60]}: {msg}")
            continue
        pair = _extract(entry, source)
        if pair:
            out[source] = pair
    return out


def create_links_http(
    urls: list[str], *, site: str, tag: str
) -> Optional[dict[str, dict[str, str]]]:
    """Mint links with a plain HTTP client using the saved session cookies.

    createLink is an ordinary JSON POST, so a browser shouldn't be required —
    this is the cheap path. Returns None (rather than raising) when the session
    isn't accepted, so the caller can fall back to driving a real browser.
    """
    import json as _json
    import re as _re

    import httpx

    import auth

    state_path = auth.storage_state_path(auth.AFFILIATE)
    if not state_path:
        return None

    try:
        cookies = {
            c["name"]: c["value"]
            for c in _json.loads(state_path.read_text(encoding="utf-8")).get("cookies", [])
        }
    except Exception:  # noqa: BLE001 - unreadable session, let the browser try
        return None

    from lib import mlgate

    site = site.rstrip("/")
    try:
        with httpx.Client(
            timeout=30, headers={"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"},
            cookies=cookies, follow_redirects=True,
        ) as client:
            mlgate.wait(mlgate.AFFILIATE, label="linkbuilder page")
            page = client.get(site + LINKBUILDER_URL)
            # A logged-out session is bounced to the login host; the CSRF token
            # we'd scrape from that page is worthless.
            if "/lgz/" in str(page.url) or "login" in str(page.url):
                log_step("affiliate session not accepted over HTTP; trying a browser")
                return None

            m = _re.search(r'<meta name="csrf-token"[^>]*content="([^"]+)"', page.text)
            if not m:
                return None

            mlgate.wait(mlgate.AFFILIATE, label=f"createLink x{len(urls)}")
            resp = client.post(
                site + CREATE_LINK_PATH,
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/plain, */*",
                    "x-csrf-token": m.group(1),
                    "origin": site,
                    "referer": site + LINKBUILDER_URL,
                },
                json={"urls": urls, "tag": tag},
            )
            if resp.status_code in (401, 403):
                log_step(f"createLink over HTTP returned {resp.status_code}; "
                         "falling back to a browser")
                return None
            if resp.status_code != 200:
                raise AffiliateAPIError(
                    f"createLink returned HTTP {resp.status_code}: {resp.text[:250]}"
                )
            return _parse_response(resp.json(), urls)
    except NotAnAffiliateError:
        raise
    except AffiliateAPIError:
        raise
    except Exception as e:  # noqa: BLE001 - any transport problem: try the browser
        log_warn(f"createLink over HTTP failed ({type(e).__name__}: {e}); "
                 "falling back to a browser")
        return None


def create_links(
    urls: list[str], *, site: str, tag: str, allow_browser: bool = True
) -> dict[str, dict[str, str]]:
    """Mint affiliate links, cheapest transport first.

    Plain HTTP needs no Chromium and takes about a second. The browser path is
    only there because it's the one we've seen work end to end; if HTTP proves
    reliable with a fresh session, `allow_browser=False` makes that permanent.
    """
    links = create_links_http(urls, site=site, tag=tag)
    if links is not None:
        log_step(f"createLink [http]: {len(links)}/{len(urls)} link(s) generated")
        return links

    if not allow_browser:
        raise AffiliateAPIError(
            "createLink over HTTP was rejected and the browser fallback is "
            "disabled. Re-run `./run login --role affiliate` to refresh the "
            "session, or set affiliate.allow_browser_fallback back to true."
        )

    with AffiliateLinkBuilder(site, tag) as builder:
        links = builder.create(urls)
    log_step(f"createLink [browser]: {len(links)}/{len(urls)} link(s) generated")
    return links
