"""Tiny console logging helpers (same shape as the twitter-updates project)."""

from __future__ import annotations

import sys
from datetime import datetime


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_stage(msg: str) -> None:
    print(f"\n\033[1m[{_ts()}] {msg}\033[0m", flush=True)


def log_step(msg: str) -> None:
    print(f"[{_ts()}]   {msg}", flush=True)


def log_ok(msg: str) -> None:
    print(f"[{_ts()}]   \033[32m✓\033[0m {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"[{_ts()}]   \033[33m!\033[0m {msg}", file=sys.stderr, flush=True)


def log_err(msg: str) -> None:
    print(f"[{_ts()}]   \033[31m✗\033[0m {msg}", file=sys.stderr, flush=True)
