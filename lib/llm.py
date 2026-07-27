"""
Chat completion with automatic provider fallback. Vendored from the youtube
project (services/llm_fallback.py).

The primary provider (default Groq / llama-3.3-70b-versatile) is subject to a
per-day token/request cap. When that *daily* cap is hit, Groq returns a 429 that
won't recover until the rolling window resets. `chat_completion_with_fallback`
detects that specific case and transparently retries once on OpenAI. Per-minute
rate limits and other errors are re-raised unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from lib.log import log_step

DEFAULT_FALLBACK_MODEL = "gpt-5-nano"


def _is_daily_limit_error(err: Exception) -> bool:
    msg = str(getattr(err, "message", "") or err).lower()
    daily_markers = ("per day", "tpd", "rpd", "tokens per day", "requests per day")
    return any(marker in msg for marker in daily_markers)


def _is_openai_endpoint(base_url: Optional[str]) -> bool:
    if not base_url:
        return True  # OpenAI SDK default endpoint
    return "api.openai.com" in base_url


def _create(
    *,
    api_key: Optional[str],
    base_url: Optional[str],
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    response_format: Optional[Dict[str, Any]],
) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Install openai: pip install openai>=1.0.0") from None

    kwargs: Dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    create_kwargs: Dict[str, Any] = {"model": model, "messages": messages}
    # The gpt-5 family (reasoning models) only accepts the default temperature.
    if not model.startswith("gpt-5"):
        create_kwargs["temperature"] = temperature
    if response_format is not None:
        create_kwargs["response_format"] = response_format

    response = client.chat.completions.create(**create_kwargs)
    return (response.choices[0].message.content or "").strip()


def chat_completion_with_fallback(
    *,
    system: str,
    user: str,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    temperature: float = 0.2,
    response_format: Optional[Dict[str, Any]] = None,
    fallback_model: Optional[str] = None,
    fallback_api_key: Optional[str] = None,
    fallback_base_url: Optional[str] = None,
    label: str = "",
) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    try:
        return _create(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            temperature=temperature,
            response_format=response_format,
        )
    except Exception as err:  # noqa: BLE001 - inspect, then re-raise or fail over
        from openai import RateLimitError

        if not isinstance(err, RateLimitError) or not _is_daily_limit_error(err):
            raise

        # Resolve the fallback target. Defaults keep the historical OpenAI
        # behavior when no explicit fallback is configured.
        fb_api_key = fallback_api_key or os.environ.get("OPENAI_API_KEY")
        fb_model = fallback_model or os.environ.get("OPENAI_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
        fb_base_url = fallback_base_url  # None → OpenAI default endpoint

        # Nothing to gain if there's no fallback key, or the fallback resolves to
        # the exact same target (same model + endpoint) that just hit its cap.
        same_target = fb_model == model and (fb_base_url or "") == (base_url or "")
        if not fb_api_key or same_target:
            raise

        where = fb_base_url or "openai"
        tag = f" [{label}]" if label else ""
        log_step(
            f"[llm-fallback]{tag} primary daily limit hit "
            f"(model={model}); retrying on model={fb_model} @ {where}"
        )
        return _create(
            api_key=fb_api_key,
            base_url=fb_base_url,
            model=fb_model,
            messages=messages,
            temperature=temperature,
            response_format=response_format,
        )
