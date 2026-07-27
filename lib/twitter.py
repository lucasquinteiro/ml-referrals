"""
Twitter/X cookie loading — vendored from the twitter-updates project so this
project stays self-contained (same resolution order, same file shape).

Cookies are resolved in this order (first hit wins):
  1. env vars TWITTER_AUTH_TOKEN + TWITTER_CT0   (best for CI / GitHub Actions)
  2. env var TWITTER_COOKIES_PATH -> json file
  3. <project>/twitter-cookies.json
  4. <ai>/twitter-updates/twitter-cookies.json   (local convenience fallback)
  5. <ai>/youtube/scripts/twitter-cookies.json   (local convenience fallback)

The json file shape is: {"auth_token": "...", "ct0": "..."}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_AI_ROOT = _PROJECT_DIR.parent


def _cookie_files() -> list[Path]:
    files: list[Path] = []
    env_path = os.environ.get("TWITTER_COOKIES_PATH", "").strip()
    if env_path:
        files.append(Path(env_path))
    files.append(_PROJECT_DIR / "twitter-cookies.json")
    files.append(_AI_ROOT / "twitter-updates" / "twitter-cookies.json")
    files.append(_AI_ROOT / "youtube" / "scripts" / "twitter-cookies.json")
    return files


def load_cookie_pair() -> Optional[tuple[str, str]]:
    """Return (auth_token, ct0), or None when no credentials are available."""
    auth = os.environ.get("TWITTER_AUTH_TOKEN", "").strip()
    ct0 = os.environ.get("TWITTER_CT0", "").strip()
    if auth and ct0:
        return auth, ct0

    for path in _cookie_files():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            auth = (data.get("auth_token") or "").strip()
            ct0 = (data.get("ct0") or "").strip()
            if auth and ct0:
                return auth, ct0
        except Exception:  # noqa: BLE001 - try the next candidate file
            continue
    return None


def load_cookies() -> Optional[str]:
    """Return a 'auth_token=...; ct0=...' cookie string, or None."""
    pair = load_cookie_pair()
    return f"auth_token={pair[0]}; ct0={pair[1]}" if pair else None
