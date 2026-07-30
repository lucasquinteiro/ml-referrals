"""
Mercado Libre read API: OAuth token management + a capability probe.

ML's read endpoints need a bearer token minted from a refresh token. Refresh
tokens are SINGLE-USE and rotate: every refresh returns a new one and kills the
old. So we persist the latest to state/ and reseed from ML_REFRESH_TOKEN only
when nothing is saved yet. On the always-on droplet state/ survives between
runs, so the rotation chain never breaks.

Getting the first refresh token is a one-time manual step (browser authorize →
code → exchange) — see the project notes; it can't be automated, it needs a
human login to the ML account.

Env:
  ML_CLIENT_ID       app id        (required)
  ML_CLIENT_SECRET   secret key    (required)
  ML_REFRESH_TOKEN   seed refresh token (required on first run; rotated after)

Usage:
  from ml_api import get            # authenticated GET against api.mercadolibre.com
  r = get("/users/me")

  python ml_api.py probe           # report which endpoints this token can reach
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import config as cfg

API = "https://api.mercadolibre.com"
_TOKEN_PATH = cfg.STATE_DIR / "ml_api_token.json"
# 6h access-token life; refresh a minute early so a call never races expiry.
_SKEW_SEC = 60


class MLApiError(RuntimeError):
    pass


def _load_saved() -> dict[str, Any]:
    if _TOKEN_PATH.is_file():
        try:
            return json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt cache just forces a refresh
            return {}
    return {}


def _save(data: dict[str, Any]) -> None:
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(json.dumps(data), encoding="utf-8")


def _creds() -> tuple[str, str]:
    cid = os.environ.get("ML_CLIENT_ID", "").strip()
    secret = os.environ.get("ML_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise MLApiError(
            "ML_CLIENT_ID / ML_CLIENT_SECRET are not set. Add them to .env "
            "(see ml_api.py docstring)."
        )
    return cid, secret


def _refresh(refresh_token: str) -> dict[str, Any]:
    import httpx

    cid, secret = _creds()
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{API}/oauth/token",
            headers={"accept": "application/json",
                     "content-type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "client_id": cid,
                  "client_secret": secret, "refresh_token": refresh_token},
        )
    if r.status_code != 200:
        raise MLApiError(
            f"token refresh failed (HTTP {r.status_code}): {r.text[:300]}. "
            "The refresh token may be spent or revoked — re-run the one-time "
            "authorize step to mint a new ML_REFRESH_TOKEN."
        )
    return r.json()


def get_access_token(*, force: bool = False) -> str:
    """A valid bearer token, refreshing (and persisting the rotation) as needed."""
    saved = _load_saved()
    now = time.time()
    if not force and saved.get("access_token") and saved.get("expires_at", 0) - now > _SKEW_SEC:
        return saved["access_token"]

    refresh_token = saved.get("refresh_token") or os.environ.get("ML_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        raise MLApiError(
            "No refresh token available. Set ML_REFRESH_TOKEN in .env (the "
            "one-time authorize step produces it)."
        )

    tok = _refresh(refresh_token)
    saved = {
        "access_token": tok["access_token"],
        # ML rotates the refresh token on every use — persist the NEW one, or the
        # next run authenticates with a token ML has already invalidated.
        "refresh_token": tok.get("refresh_token", refresh_token),
        "expires_at": now + int(tok.get("expires_in", 21600)),
        "user_id": tok.get("user_id"),
        "scope": tok.get("scope"),
    }
    _save(saved)
    return saved["access_token"]


def get(path: str, **params: Any):
    """Authenticated GET against the ML API. Returns the httpx.Response."""
    import httpx

    token = get_access_token()
    with httpx.Client(timeout=30) as c:
        return c.get(f"{API}{path}", params=params or None,
                     headers={"Authorization": f"Bearer {token}"})


# --------------------------------------------------------------------------
# Probe: report exactly which read paths this token can reach. This is what
# settles the open question — does the category `search` endpoint 403 for our
# app, the way it does anonymously and the way other developers report?
# --------------------------------------------------------------------------
def probe(site: str = "MLA") -> int:
    checks: list[tuple[str, str, dict[str, Any]]] = [
        ("users/me (token sanity)", "/users/me", {}),
        ("domain_discovery (open)", f"/sites/{site}/domain_discovery/search",
         {"q": "creatina", "limit": 1}),
        ("search q= (expected 403)", f"/sites/{site}/search", {"q": "creatina", "limit": 1}),
        ("search category=", f"/sites/{site}/search", {"category": "MLA3551", "limit": 1}),
        ("search category+attribute", f"/sites/{site}/search",
         {"category": "MLA3551", "MAIN_SUPPLEMENT": "6565367", "limit": 1}),
    ]
    print("Probing the ML read API with the configured token...\n")
    ok = True
    for label, path, params in checks:
        try:
            r = get(path, **params)
            body = r.text[:160].replace("\n", " ")
            flag = "OK " if r.status_code == 200 else "!! "
            if r.status_code != 200 and "search" in path and "category" in params:
                ok = False
            print(f"  {flag}{label:32} HTTP {r.status_code}  {body}")
        except MLApiError as e:
            print(f"  !! {label:32} {e}")
            return 1
    print("\nVerdict:", "category search WORKS — safe to build API ingest."
          if ok else "category search is blocked — API can't replace the scrape; "
          "keep /ofertas + screenshots.")
    return 0


if __name__ == "__main__":
    import sys

    cfg.bootstrap()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if cmd == "probe":
        raise SystemExit(probe())
    if cmd == "token":
        print(get_access_token())
        raise SystemExit(0)
    print(f"unknown command: {cmd} (try: probe | token)")
    raise SystemExit(2)
