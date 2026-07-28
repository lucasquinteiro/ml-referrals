"""
Slack Incoming Webhook helper. Vendored from the youtube project.
"""

from __future__ import annotations

import os
from typing import Optional


def _default_webhook_url() -> str:
    return (os.getenv("SLACK_WEBHOOK_URL") or "").strip()


def send_slack_message(
    text: str,
    *,
    webhook_url: Optional[str] = None,
    timeout_seconds: float = 10.0,
) -> bool:
    """
    Send a plain text message to Slack via Incoming Webhook.
    Returns True if posted, False when the webhook URL is not configured.
    Raises on HTTP/network errors so callers can decide whether to fail or ignore.
    """
    url = (webhook_url or _default_webhook_url()).strip()
    if not url:
        return False

    import httpx

    r = httpx.post(url, json={"text": text}, timeout=timeout_seconds)
    r.raise_for_status()
    return True
