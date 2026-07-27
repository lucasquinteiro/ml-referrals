"""
Optional MercadoLibre session, for the pages that require one.

Anonymous access covers /ofertas, which is what the pipeline runs on by default.
Search (`listado.mercadolibre.com.ar/<term>`) and product detail pages are behind
a login wall, so reaching those needs a real logged-in session.

Your password never goes near this code, and never near the agent that wrote it.
`./run login` opens a real browser window; you log in yourself (2FA included),
and Playwright saves the resulting session to state/ml-storage.json — which is
gitignored, like every other credential in this project.

Two ways to provide a session, checked in this order:
  1. ML_STORAGE_STATE_PATH -> a Playwright storage-state JSON file
  2. state/ml-storage.json  -> what `./run login` writes

A session is just cookies: it expires, and it identifies your account. See the
warning in the README before pointing this at your affiliate account.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import config as cfg
from lib.log import log_ok, log_step, log_warn

STORAGE_STATE_PATH = cfg.STATE_DIR / "ml-storage.json"

# Where the login form lives. Landing on the homepage first looks less abrupt
# than deep-linking into the auth flow.
_HOME = "/"


def storage_state_path() -> Optional[Path]:
    """The session file to use, or None when there isn't one."""
    env = os.environ.get("ML_STORAGE_STATE_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    return STORAGE_STATE_PATH if STORAGE_STATE_PATH.is_file() else None


def has_session() -> bool:
    return storage_state_path() is not None


def describe_session() -> str:
    p = storage_state_path()
    if not p:
        return "none (anonymous — /ofertas only)"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        n = len(data.get("cookies") or [])
        return f"{p} ({n} cookies)"
    except Exception:  # noqa: BLE001
        return f"{p} (unreadable)"


def login(site: str, *, timeout_min: int = 5) -> int:
    """Open a browser, wait for the user to log in, then save the session.

    Interactive by design — it needs a visible window, so it can't run in CI.
    Refresh the session by re-running it whenever scraping starts hitting the
    wall again.
    """
    from playwright.sync_api import sync_playwright

    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="es-AR",
            viewport={"width": 1280, "height": 900},
            storage_state=str(storage_state_path()) if has_session() else None,
        )
        page = ctx.new_page()
        page.goto(site.rstrip("/") + _HOME, wait_until="domcontentloaded", timeout=60_000)

        log_step("A browser window is open.")
        log_step("Log in to Mercado Libre there — take your time, 2FA is fine.")
        log_step("Nothing you type is visible to this process; only the resulting")
        log_step("session cookies are saved.")
        print()
        try:
            input("  Press Enter here once you're logged in (Ctrl-C to cancel)... ")
        except (EOFError, KeyboardInterrupt):
            log_warn("cancelled; no session saved")
            browser.close()
            return 130

        # Confirm the session actually works before saving it, so a half-finished
        # login doesn't get written out and fail silently on the next run.
        page.goto(
            "https://listado.mercadolibre.com.ar/notebook",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(4000)
        walled = "account-verification" in page.url
        ctx.storage_state(path=str(STORAGE_STATE_PATH))
        browser.close()

    if walled:
        log_warn(
            f"session saved to {STORAGE_STATE_PATH}, but a search page still "
            "redirected to the login wall. The login may not have completed — "
            "re-run `./run login`, or try `./run ingest --source search` to see."
        )
        return 1

    log_ok(f"session saved to {STORAGE_STATE_PATH} — search scraping is available")
    log_step("It's gitignored. Re-run `./run login` when it expires.")
    return 0
