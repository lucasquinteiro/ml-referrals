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

_ENV_VARS = {
    SCRAPING: "ML_STORAGE_STATE_PATH",
    AFFILIATE: "ML_AFFILIATE_STORAGE_STATE_PATH",
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


def storage_state_path(role: str = SCRAPING) -> Optional[Path]:
    """The session file to use for `role`, or None when there isn't one."""
    env = os.environ.get(_ENV_VARS.get(role, ""), "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None

    path = session_file(role)
    if path.is_file():
        return path
    if role == SCRAPING and LEGACY_PATH.is_file():
        return LEGACY_PATH
    return None


def has_session(role: str = SCRAPING) -> bool:
    return storage_state_path(role) is not None


def session_account(role: str = SCRAPING) -> Optional[dict[str, str]]:
    """Which Mercado Libre account a saved session belongs to.

    ML stores the nickname and user id in cookies, so the two roles can be told
    apart at a glance — the failure mode this split exists to prevent is
    silently having both point at the same account.
    """
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


def describe_session(role: str = SCRAPING) -> str:
    p = storage_state_path(role)
    if not p:
        if role == SCRAPING:
            return "none (anonymous — /ofertas only)"
        return f"none (run `./run login --role {role}`)"

    acct = session_account(role) or {}
    who = acct.get("nickname") or "?"
    uid = acct.get("user_id")
    return f"{who}{f' (id {uid})' if uid else ''}"


def describe_all() -> str:
    return " | ".join(f"{r}: {describe_session(r)}" for r in ROLES)


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


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
    """Open a browser, wait for the user to log in, verify, then save."""
    from playwright.sync_api import sync_playwright

    if role not in ROLES:
        log_err(f"Unknown role '{role}'. Use one of: {', '.join(ROLES)}")
        return 1

    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = session_file(role)

    log_step(f"Logging in for the \033[1m{role}\033[0m session — "
             f"{_DESCRIPTIONS[role]}.")
    if role == SCRAPING and has_session(AFFILIATE):
        log_step("Tip: sign in with a *different* account than your affiliate one.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"]
        )
        # Deliberately a clean context: reusing the other role's cookies is how
        # you end up saving the same account twice without noticing.
        ctx = browser.new_context(locale="es-AR", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(site.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60_000)

        log_step("A browser window is open. Log in to Mercado Libre there.")
        log_step("Nothing you type is visible to this process; only the resulting")
        log_step("session cookies are saved.")
        print()
        try:
            input("  Press Enter here once you're logged in (Ctrl-C to cancel)... ")
        except (EOFError, KeyboardInterrupt):
            log_warn("cancelled; no session saved")
            browser.close()
            return 130

        try:
            ok, detail = (
                _verify_affiliate(page, site) if role == AFFILIATE
                else _verify_scraping(page)
            )
        except Exception as e:  # noqa: BLE001 - save anyway, just say so
            ok, detail = True, f"could not verify ({type(e).__name__}: {e})"

        ctx.storage_state(path=str(target))
        browser.close()

    if not ok:
        log_warn(f"session saved to {target.name}, but {detail}")
        return 1

    log_ok(f"{role} session saved to state/{target.name}")
    log_step(f"account: {describe_session(role)}")
    log_step(detail)

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
