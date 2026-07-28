"""
Mercado Libre sessions, one per role.

Two different jobs want two different accounts:

  scraping   — hammers search pages behind ML's login wall. Use a burner: this
               is the account that risks getting rate-limited or flagged.
  affiliate  — calls the link builder to mint real meli.la links. Must be the
               account actually enrolled in the Programa de Afiliados, because
               that's whose commission the links credit.

Keeping them apart means the account your earnings depend on never touches the
automated scraping. Nothing stops you pointing both at one account, but the
whole reason for the split is that you shouldn't.

Your password never goes near this code. `./run login --role <role>` opens a
real browser window; you log in yourself (2FA included), and Playwright saves
the resulting cookies to state/, which is gitignored.

Resolution per role (first hit wins):
  1. the role's env var  (ML_STORAGE_STATE_PATH / ML_AFFILIATE_STORAGE_STATE_PATH)
  2. state/ml-session-<role>.json
  3. scraping only: state/ml-storage.json, the pre-split filename
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import config as cfg
from lib.log import log_err, log_ok, log_step, log_warn

SCRAPING = "scraping"
AFFILIATE = "affiliate"
ROLES = (SCRAPING, AFFILIATE)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# A live browser profile directory per role — the persistent alternative to a
# frozen storage_state snapshot. Chromium reads *and writes* this as it browses,
# so Mercado Libre's rotating tokens are kept across runs instead of being
# replayed stale. This is what a `./run login` creates now.
_PROFILE_DIR = cfg.STATE_DIR / "profiles"

_ENV_VARS = {
    SCRAPING: "ML_STORAGE_STATE_PATH",
    AFFILIATE: "ML_AFFILIATE_STORAGE_STATE_PATH",
}

# The session JSON itself, rather than a path — this is how a session reaches
# GitHub Actions, where there's no browser to log in with. Paste the contents
# of state/ml-session-<role>.json into the matching repository secret.
_ENV_CONTENT_VARS = {
    SCRAPING: "ML_STORAGE_STATE",
    AFFILIATE: "ML_AFFILIATE_STORAGE_STATE",
}

# Written before the roles were split; still honoured for scraping so an
# existing setup keeps working without a re-login.
LEGACY_PATH = cfg.STATE_DIR / "ml-storage.json"

_DESCRIPTIONS = {
    SCRAPING: "scraping search pages (use a burner account)",
    AFFILIATE: "generating affiliate links (must be your affiliate account)",
}


def session_file(role: str) -> Path:
    return cfg.STATE_DIR / f"ml-session-{role}.json"


def profile_dir(role: str) -> Path:
    return _PROFILE_DIR / role


def has_profile(role: str) -> bool:
    """True when a persistent browser profile exists for this role."""
    p = profile_dir(role)
    # Chromium writes a "Default" subdir and cookie DB into a real profile.
    return p.is_dir() and any(p.iterdir())


def storage_state_path(role: str = SCRAPING) -> Optional[Path]:
    """The session file to use for `role`, or None when there isn't one."""
    env = os.environ.get(_ENV_VARS.get(role, ""), "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None

    # A session supplied as JSON (CI secret) is materialised to disk, since
    # Playwright only accepts a file path.
    blob = os.environ.get(_ENV_CONTENT_VARS.get(role, ""), "").strip()
    if blob:
        materialised = cfg.STATE_DIR / f"ml-session-{role}-env.json"
        try:
            json.loads(blob)  # fail fast on a mangled secret
        except json.JSONDecodeError as e:
            log_warn(f"{_ENV_CONTENT_VARS[role]} is not valid JSON ({e}); ignoring")
            return None
        cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        materialised.write_text(blob, encoding="utf-8")
        return materialised

    path = session_file(role)
    if path.is_file():
        return path
    if role == SCRAPING and LEGACY_PATH.is_file():
        return LEGACY_PATH
    return None


def has_session(role: str = SCRAPING) -> bool:
    """True when this role has *any* usable session — profile or snapshot."""
    return has_profile(role) or storage_state_path(role) is not None


class BrowserSession:
    """A Chromium context for a role, with the best available session backing.

    Resolution, best-first:
      1. persistent profile dir  — Chromium keeps ML's rotating tokens fresh
      2. storage_state snapshot  — the legacy frozen JSON (still honoured)
      3. anonymous               — role=None, or nothing saved

    Unifies the three launch shapes so the scraper, screenshot and link builder
    don't each re-implement the profile-vs-snapshot choice. Use as a context
    manager; `.context` is the Playwright BrowserContext.

        with BrowserSession(auth.SCRAPING, viewport={...}) as ctx:
            page = ctx.new_page()
    """

    def __init__(
        self,
        role: Optional[str] = None,
        *,
        headless: bool = True,
        launch_args: Optional[list[str]] = None,
        **context_kwargs,
    ) -> None:
        self.role = role
        self.headless = headless
        self.launch_args = launch_args or [
            "--disable-blink-features=AutomationControlled", "--no-sandbox"
        ]
        self.context_kwargs = context_kwargs
        self.context = None
        self._pw = None
        self._browser = None
        self.mode = "anonymous"

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = {"locale": "es-AR", "user_agent": UA, **self.context_kwargs}

        if self.role and has_profile(self.role):
            self.mode = "profile"
            # Persistent context: one call, no separate browser object.
            self.context = self._pw.chromium.launch_persistent_context(
                str(profile_dir(self.role)),
                headless=self.headless,
                args=self.launch_args,
                **kwargs,
            )
        else:
            self._browser = self._pw.chromium.launch(
                headless=self.headless, args=self.launch_args
            )
            state = storage_state_path(self.role) if self.role else None
            self.mode = "snapshot" if state else "anonymous"
            self.context = self._browser.new_context(
                storage_state=str(state) if state else None, **kwargs
            )
        return self.context

    def __exit__(self, *exc):
        for closer in (self.context if self._browser is None else None,
                       self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def session_account(role: str = SCRAPING) -> Optional[dict[str, str]]:
    """Which Mercado Libre account a saved session belongs to.

    ML stores the nickname and user id in cookies, so the two roles can be told
    apart at a glance — the failure mode this split exists to prevent is
    silently having both point at the same account.
    """
    # A profile dir has no readable JSON (Chromium encrypts its cookie DB), so
    # login drops a small sidecar naming the account it signed in as.
    sidecar = profile_dir(role) / "account.json"
    if sidecar.is_file():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8")) or None
        except Exception:  # noqa: BLE001
            pass

    p = storage_state_path(role)
    if not p:
        return None
    try:
        cookies = json.loads(p.read_text(encoding="utf-8")).get("cookies") or []
    except Exception:  # noqa: BLE001
        return None

    found: dict[str, str] = {}
    for c in cookies:
        if c.get("name") == "orgnickp" and c.get("value"):
            found["nickname"] = c["value"]
        elif c.get("name") == "orguseridp" and c.get("value"):
            found["user_id"] = c["value"]
    return found or None


def _write_account_sidecar(role: str, context) -> None:
    """Record which account a fresh profile belongs to, for describe_session."""
    acct: dict[str, str] = {}
    for c in context.cookies():
        if c.get("name") == "orgnickp" and c.get("value"):
            acct["nickname"] = c["value"]
        elif c.get("name") == "orguseridp" and c.get("value"):
            acct["user_id"] = c["value"]
    if acct:
        (profile_dir(role) / "account.json").write_text(
            json.dumps(acct), encoding="utf-8"
        )


def describe_session(role: str = SCRAPING) -> str:
    if not has_session(role):
        if role == SCRAPING:
            return "none (anonymous — /ofertas only)"
        return f"none (run `./run login --role {role}`)"

    kind = "profile" if has_profile(role) else "snapshot"
    acct = session_account(role) or {}
    who = acct.get("nickname") or "?"
    uid = acct.get("user_id")
    return f"{who}{f' (id {uid})' if uid else ''} [{kind}]"


def describe_all() -> str:
    return " | ".join(f"{r}: {describe_session(r)}" for r in ROLES)


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


def session_check(site: str, role: str) -> dict:
    """Open the saved session and confirm it's still logged in.

    The round-trip test: reopen the profile from disk (a fresh process, exactly
    like a scheduled run), hit a page that only renders when authenticated, and
    report. Run it on a timer to measure how long a session actually lives —
    the only real test of longevity.

    Returns {"alive", "role", "account", "mode", "detail"}.
    """
    from lib import mlgate

    result = {"role": role, "alive": False, "account": describe_session(role),
              "mode": "profile" if has_profile(role) else "snapshot", "detail": ""}
    if not has_session(role):
        result["detail"] = "no session saved"
        return result

    check_url = (site.rstrip("/") + "/afiliados/linkbuilder" if role == AFFILIATE
                 else "https://listado.mercadolibre.com.ar/notebook")
    gate_acct = mlgate.AFFILIATE if role == AFFILIATE else mlgate.SCRAPING

    try:
        with BrowserSession(role, viewport={"width": 1280, "height": 900}) as ctx:
            page = ctx.new_page()
            mlgate.wait(gate_acct, label="session-check")
            page.goto(check_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3500)
            walled = "account-verification" in page.url or "/lgz/" in page.url
            result["alive"] = not walled
            result["detail"] = ("logged in" if not walled
                                else "bounced to the login wall — session is dead")
    except Exception as e:  # noqa: BLE001
        result["detail"] = f"check failed ({type(e).__name__}: {e})"
    return result


def _verify_scraping(page) -> tuple[bool, str]:
    """A search page must render instead of bouncing to the login wall."""
    page.goto(
        "https://listado.mercadolibre.com.ar/notebook",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    page.wait_for_timeout(4000)
    if "account-verification" in page.url:
        return False, "a search page still redirected to the login wall"
    return True, "search pages load — `./run ingest --source search` will work"


def _verify_affiliate(page, site: str) -> tuple[bool, str]:
    """The account must actually be enrolled — check by minting a test link.

    Catches the easy mistake of logging in with the wrong account here, rather
    than at post time when it costs a publish.
    """
    from affiliate_api import CREATE_LINK_PATH, _CREATE_JS

    tag = cfg.load_settings().affiliate_tag
    if not tag:
        return True, ("logged in, but no affiliate tag configured yet — run "
                      "`./run set-affiliate <link>` to finish setup")

    page.goto(site.rstrip("/") + "/afiliados/linkbuilder",
              wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)

    res = page.evaluate(_CREATE_JS, {
        "path": CREATE_LINK_PATH,
        "urls": [f"{site.rstrip('/')}/p/MLA69985783"],
        "tag": tag,
    })
    body = res.get("body") or {}
    entries = body.get("urls") or []
    for entry in entries:
        msg = entry.get("message") or ""
        if entry.get("error_code") == 109 or "not found affiliate user" in msg:
            return False, (
                f"this account isn't enrolled as an affiliate ({msg}). "
                "Sign in with the account that owns the Programa de Afiliados."
            )
    return True, f"affiliate link generation works for tag '{tag}'"


def login(site: str, role: str = SCRAPING) -> int:
    """Open a browser, wait for the user to log in, verify, then save.

    Saves a *persistent profile* now (state/profiles/<role>), not a frozen
    snapshot. Chromium keeps that profile's cookies current on every later run,
    the way a real logged-in browser does — the fix for sessions dying in a day.
    """
    from playwright.sync_api import sync_playwright

    if role not in ROLES:
        log_err(f"Unknown role '{role}'. Use one of: {', '.join(ROLES)}")
        return 1

    target = profile_dir(role)
    target.mkdir(parents=True, exist_ok=True)

    log_step(f"Logging in for the \033[1m{role}\033[0m session — "
             f"{_DESCRIPTIONS[role]}.")
    if role == SCRAPING and has_session(AFFILIATE):
        log_step("Tip: sign in with a *different* account than your affiliate one.")

    with sync_playwright() as p:
        # A persistent context IS the profile on disk — no storage_state.
        ctx = p.chromium.launch_persistent_context(
            str(target), headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            locale="es-AR", viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(site.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60_000)

        log_step("A browser window is open. Log in to Mercado Libre there.")
        log_step("Nothing you type is visible to this process; the login lives in")
        log_step("the profile on disk, which Chromium keeps refreshed from now on.")
        print()
        try:
            input("  Press Enter here once you're logged in (Ctrl-C to cancel)... ")
        except (EOFError, KeyboardInterrupt):
            log_warn("cancelled")
            ctx.close()
            return 130

        try:
            ok, detail = (
                _verify_affiliate(page, site) if role == AFFILIATE
                else _verify_scraping(page)
            )
        except Exception as e:  # noqa: BLE001 - save anyway, just say so
            ok, detail = True, f"could not verify ({type(e).__name__}: {e})"

        _write_account_sidecar(role, ctx)
        ctx.close()

    if not ok:
        log_warn(f"profile saved to state/profiles/{role}/, but {detail}")
        return 1

    log_ok(f"{role} session saved as a persistent profile (state/profiles/{role}/)")
    log_step(f"account: {describe_session(role)}")
    log_step(detail)

    log_step("It's a live profile, not a snapshot — it refreshes itself. "
             "Re-run only if it ever gets signed out.")

    other = AFFILIATE if role == SCRAPING else SCRAPING
    this_acct = (session_account(role) or {}).get("user_id")
    other_acct = (session_account(other) or {}).get("user_id")
    if this_acct and this_acct == other_acct:
        log_warn(
            f"the {other} session is the SAME account (id {this_acct}).\n"
            "  That defeats the point of splitting them — scraping is what gets\n"
            f"  accounts flagged, and this is the account your commissions depend\n"
            f"  on. Re-run `./run login --role {SCRAPING}` with a burner."
        )

    log_step("It's gitignored. Re-run this when it expires.")
    return 0
