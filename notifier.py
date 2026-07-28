"""
Slack summary of an ingest run.

Posts what the run actually found, so a scheduled job is legible without
opening the logs: how much was scraped, what matched, the best deals, and the
state of the queue waiting to be tweeted.

Silent no-op when SLACK_WEBHOOK_URL isn't set, and never fatal — a failed
notification must not fail an otherwise good ingest.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import log_ok, log_step, log_warn


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"${int(round(value)):,}".replace(",", ".")


def _line(product: Any) -> str:
    return (
        f"• *{product.discount_pct}% OFF* — {product.title[:70]}\n"
        f"   {_fmt_price(product.original_price)} → *{_fmt_price(product.price)}*"
        f"  ·  _{product.matched_label}_"
    )


def build_summary(
    *,
    site: str,
    source: str,
    pages: int,
    scraped: int,
    matched: int,
    deals: list[Any],
    queue_total: Optional[int] = None,
    per_label: Optional[dict[str, int]] = None,
    top_n: int = 5,
    dry_run: bool = False,
) -> str:
    """Human-readable Slack message for one ingest run."""
    head = "🧪 *ml-referrals — ingest (dry run)*" if dry_run else "🛒 *ml-referrals — ingest*"
    lines = [
        head,
        f"`{source}` · {pages} page(s) · {site.replace('https://www.', '')}",
        "",
        f"*{scraped}* scraped → *{matched}* matched a keyword → *{len(deals)}* clear the thresholds",
    ]

    if queue_total is not None and queue_total != len(deals):
        lines.append(f"Queue waiting to be tweeted: *{queue_total}*")

    if per_label:
        top = sorted(per_label.items(), key=lambda kv: kv[1], reverse=True)[:8]
        lines.append("_" + " · ".join(f"{k}: {v}" for k, v in top) + "_")

    if deals:
        lines += ["", f"*Top {min(top_n, len(deals))} deals:*"]
        lines += [_line(p) for p in deals[:top_n]]
    else:
        lines += ["", "_Nothing cleared the thresholds this run._"]

    return "\n".join(lines)


def notify(text: str, *, webhook_url: Optional[str] = None) -> bool:
    """Post to Slack. Returns False when unconfigured; never raises."""
    from lib.slack import send_slack_message

    try:
        sent = send_slack_message(text, webhook_url=webhook_url)
    except Exception as e:  # noqa: BLE001 - a failed ping must not fail the run
        log_warn(f"Slack notification failed ({type(e).__name__}: {e})")
        return False

    if sent:
        log_ok("posted ingest summary to Slack")
    else:
        log_step("SLACK_WEBHOOK_URL not set — skipping Slack summary")
    return sent
