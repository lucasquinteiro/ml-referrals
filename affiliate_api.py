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
  let body = null;
  try { body = await r.json(); } catch (e) { body = {parseError: await r.text()}; }
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
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self) -> "AffiliateLinkBuilder":
        import auth
        from playwright.sync_api import sync_playwright

        state = self.storage_state or auth.storage_state_path(auth.AFFILIATE)
        if not state:
            raise AffiliateAPIError(
                "No affiliate session. Run `./run login --role affiliate` and "
                "sign in with the account enrolled in the Programa de Afiliados."
            )

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = self._browser.new_context(
            locale="es-AR", user_agent=UA, storage_state=str(state)
        )
        self._page = ctx.new_page()
        self._page.goto(
            self.site + LINKBUILDER_URL, wait_until="domcontentloaded", timeout=60_000
        )
        self._page.wait_for_timeout(3000)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    def create(self, urls: list[str]) -> dict[str, dict[str, str]]:
        """Generate links for `urls`. Returns {input_url: {"short", "full"}}.

        Missing entries mean that URL failed; the caller falls back to the
        param form for those rather than dropping the product.
        """
        if not urls:
            return {}
        assert self._page is not None, "use as a context manager"

        res = self._page.evaluate(
            _CREATE_JS, {"path": CREATE_LINK_PATH, "urls": urls, "tag": self.tag}
        )
        body = res.get("body") or {}

        if res.get("httpStatus") != 200:
            raise AffiliateAPIError(
                f"createLink returned HTTP {res.get('httpStatus')}: {str(body)[:300]}"
            )

        entries = body.get("urls") or []
        out: dict[str, dict[str, str]] = {}
        for i, entry in enumerate(entries):
            # Success entries name the input as `origin_url`; error entries use
            # `entity`. Fall back to position, since the response preserves the
            # order of the request.
            source = (
                entry.get("origin_url")
                or entry.get("entity")
                or (urls[i] if i < len(urls) else "")
            )
            if entry.get("error_code") or entry.get("message"):
                msg = entry.get("message", "")
                # 109 = the session's account isn't an affiliate. Worth failing
                # loudly: every link in the batch will fail the same way.
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

        if entries and not out:
            log_warn(f"createLink returned no usable links: {str(body)[:200]}")
        return out


def create_links(
    urls: list[str], *, site: str, tag: str
) -> dict[str, dict[str, str]]:
    """One-shot convenience wrapper around AffiliateLinkBuilder."""
    with AffiliateLinkBuilder(site, tag) as builder:
        links = builder.create(urls)
    log_step(f"createLink: {len(links)}/{len(urls)} link(s) generated")
    return links
