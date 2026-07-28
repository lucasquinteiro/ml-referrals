"""
Global rate gate for every Mercado Libre request.

The one rule that protects the accounts: nothing touches Mercado Libre without
passing through here first. Scraping, card screenshots, link minting — all of it
calls `wait(account)` before the request goes out. So no command can decide on
its own to burst, and it doesn't matter whether one job runs or several fire
back to back from cron: their requests serialise through a single throttle.

State lives in files under state/mlgate/ so it holds *across processes*. Cron
starts each command as a fresh process; a throttle kept in memory would reset
every time and enforce nothing. The files are guarded by an flock, so concurrent
processes see one consistent view.

Three mechanisms:

  1. min interval  — a jittered floor on the gap between any two requests, so
                     the cadence never looks mechanical.
  2. budget        — a rolling cap per hour and per day, per account. When it's
                     spent the account blocks until the window rolls forward.
                     The hard ceiling a runaway loop cannot exceed.
  3. circuit break — `trip(account)` opens a cooldown during which every request
                     for that account blocks. Callers trip it the instant they
                     see a wall or challenge, so a soft flag isn't hammered into
                     a hard ban.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import time
from pathlib import Path
from typing import Any

import config as cfg
from lib.log import log_step, log_warn

_GATE_DIR = cfg.STATE_DIR / "mlgate"

# The three ML identities the gate meters, by what's actually at risk:
#   ANON       anonymous /ofertas requests — no session, so IP throttle only
#   SCRAPING   the burner session on search listings
#   AFFILIATE  the affiliate account on the link builder
ANON = "anon"
SCRAPING = "scraping"
AFFILIATE = "affiliate"

# Defaults; config.json "ml_gate" overrides. Budgets are per account, because
# the burner does the bulk of the work while the affiliate account should barely
# be touched.
_DEFAULTS: dict[str, Any] = {
    "min_interval_sec": 45.0,
    "jitter": 0.5,               # +/-50% around min_interval
    "max_per_hour": {"anon": 120, "scraping": 40, "affiliate": 8},
    "max_per_day": {"anon": 800, "scraping": 200, "affiliate": 30},
    "cooldown_sec": 3600,        # circuit-breaker hold after a wall
    "max_single_wait_sec": 900,  # never sleep longer than this in one hop
}


def _settings() -> dict[str, Any]:
    data = dict(_DEFAULTS)
    try:
        user = (cfg.load_settings().get("ml_gate") or {})
    except Exception:  # noqa: BLE001 - fall back to defaults if config is unreadable
        user = {}
    data.update({k: v for k, v in user.items() if k != "max_per_hour" and k != "max_per_day"})
    for k in ("max_per_hour", "max_per_day"):
        if isinstance(user.get(k), dict):
            merged = dict(_DEFAULTS[k])
            merged.update(user[k])
            data[k] = merged
    return data


def _state_path(account: str) -> Path:
    return _GATE_DIR / f"{account}.json"


def _load(account: str) -> dict[str, Any]:
    path = _state_path(account)
    if not path.is_file():
        return {"last_ts": 0.0, "history": [], "cooldown_until": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("last_ts", 0.0)
        data.setdefault("history", [])
        data.setdefault("cooldown_until", 0.0)
        return data
    except Exception:  # noqa: BLE001 - corrupt state resets rather than crashes
        return {"last_ts": 0.0, "history": [], "cooldown_until": 0.0}


def _save(account: str, state: dict[str, Any]) -> None:
    _state_path(account).write_text(json.dumps(state), encoding="utf-8")


class _FileLock:
    """Blocking flock on a per-account lockfile. Works on macOS and Linux."""

    def __init__(self, account: str) -> None:
        self._path = _GATE_DIR / f"{account}.lock"
        self._fh = None

    def __enter__(self):
        _GATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()


def _prune(history: list[float], now: float) -> list[float]:
    """Drop request timestamps older than a day."""
    return [t for t in history if now - t < 86400]


def _budget_wait(history: list[float], now: float, cfg_gate: dict[str, Any],
                 account: str) -> float:
    """Seconds to wait for the hourly/daily budget to free a slot (0 if free)."""
    per_hour = (cfg_gate["max_per_hour"] or {}).get(account)
    per_day = (cfg_gate["max_per_day"] or {}).get(account)
    wait = 0.0

    if per_hour:
        in_hour = sorted(t for t in history if now - t < 3600)
        if len(in_hour) >= per_hour:
            # Wait until the oldest request in the window ages out.
            wait = max(wait, in_hour[-per_hour] + 3600 - now)
    if per_day:
        in_day = sorted(t for t in history if now - t < 86400)
        if len(in_day) >= per_day:
            wait = max(wait, in_day[-per_day] + 86400 - now)
    return wait


def wait(account: str, *, label: str = "") -> None:
    """Block until it's this account's turn to make one Mercado Libre request.

    Records the request on return, so the *next* caller sees it. Every ML touch
    must call this immediately before the request.
    """
    g = _settings()
    deadline_note_shown = False

    while True:
        with _FileLock(account):
            now = time.time()
            state = _load(account)
            state["history"] = _prune(state["history"], now)

            waits = []
            cd = state.get("cooldown_until", 0.0) - now
            if cd > 0:
                waits.append(("cooldown", cd))

            interval = g["min_interval_sec"] * random.uniform(
                1 - g["jitter"], 1 + g["jitter"]
            )
            gap = state["last_ts"] + interval - now
            if gap > 0:
                waits.append(("interval", gap))

            bwait = _budget_wait(state["history"], now, g, account)
            if bwait > 0:
                waits.append(("budget", bwait))

            if not waits:
                state["last_ts"] = now
                state["history"].append(now)
                _save(account, state)
                return

            reason, longest = max(waits, key=lambda kv: kv[1])
            _save(account, state)  # persist any pruning

        sleep_for = min(longest, g["max_single_wait_sec"])
        if reason in ("cooldown", "budget") and not deadline_note_shown:
            log_warn(f"ml-gate[{account}]: {reason} — waiting "
                     f"{longest:.0f}s{f' ({label})' if label else ''}")
            deadline_note_shown = True
        time.sleep(max(0.2, sleep_for))


def trip(account: str, reason: str = "wall") -> None:
    """Open the circuit breaker: block this account for the cooldown window."""
    g = _settings()
    with _FileLock(account):
        state = _load(account)
        state["cooldown_until"] = time.time() + g["cooldown_sec"]
        _save(account, state)
    log_warn(f"ml-gate[{account}]: tripped ({reason}); pausing all requests for "
             f"{g['cooldown_sec'] // 60} min")


def status(account: str) -> dict[str, Any]:
    """Current gate state for an account — for `./run` reporting."""
    now = time.time()
    state = _load(account)
    history = _prune(state["history"], now)
    return {
        "last_request_ago_sec": round(now - state["last_ts"]) if state["last_ts"] else None,
        "in_last_hour": len([t for t in history if now - t < 3600]),
        "in_last_day": len(history),
        "cooldown_remaining_sec": max(0, round(state.get("cooldown_until", 0) - now)),
    }
