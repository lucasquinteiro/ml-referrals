"""
Supabase-backed store. Drop-in replacement for the SQLite `Store` in store.py —
same method surface, so run.py doesn't care which backend it talks to.

Switch to it with "store": "supabase" in config.json. That's what GitHub Actions
needs: an Actions runner is ephemeral, so a SQLite file in state/ would vanish
between runs and the price history would never accumulate.

Tables (created by supabase_schema.sql):
  mlr_products, mlr_price_history, mlr_posts, mlr_runs
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

_PAGE = 1000  # PostgREST default max rows per request


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    def __init__(self) -> None:
        from supabase import create_client

        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. Set them in "
                ".env (or as GitHub Actions secrets), or switch config.json "
                '"store" back to "sqlite".'
            )
        self.sb = create_client(url, key)

    # ---- runs ------------------------------------------------------------

    def start_run(self, kind: str, note: str = "") -> Optional[int]:
        res = self.sb.table("mlr_runs").insert(
            {"started_at": _now_iso(), "kind": kind, "note": note}
        ).execute()
        rows = res.data or []
        return rows[0]["id"] if rows else None

    def finish_run(self, run_id: Optional[int], *, products_seen: int, offers_matched: int) -> None:
        if run_id is None:
            return
        self.sb.table("mlr_runs").update(
            {"products_seen": products_seen, "offers_matched": offers_matched}
        ).eq("id", run_id).execute()

    # ---- products + price snapshots -------------------------------------

    def record_snapshots(self, products: Iterable[Any], run_id: Optional[int] = None) -> int:
        products = list(products)
        if not products:
            return 0
        now = _now_iso()

        product_rows = [
            {
                "product_id": p.product_id,
                "title": p.title,
                "url": p.url,
                "image": p.image,
                "seller": p.seller,
                "matched_keyword": p.matched_keyword,
                "matched_label": p.matched_label,
                "first_seen": now,
                "last_seen": now,
            }
            for p in products
        ]
        price_rows = [
            {
                "product_id": p.product_id,
                "observed_at": now,
                "price": p.price,
                "original_price": p.original_price,
                "discount_pct": p.discount_pct,
                "currency": p.currency,
                "badge": p.badge,
                "free_shipping": p.free_shipping,
                "rating": p.rating,
                "run_id": run_id,
            }
            for p in products
        ]

        for i in range(0, len(product_rows), _PAGE):
            # Upsert so repeat sightings refresh last_seen/title but keep the
            # original first_seen semantics handled by the caller's ordering.
            self.sb.table("mlr_products").upsert(
                product_rows[i : i + _PAGE], on_conflict="product_id"
            ).execute()
        for i in range(0, len(price_rows), _PAGE):
            self.sb.table("mlr_price_history").insert(price_rows[i : i + _PAGE]).execute()

        return len(products)

    def record_snapshot(self, product: Any, run_id: Optional[int] = None) -> None:
        self.record_snapshots([product], run_id)

    def price_history(self, product_id: str, limit: int = 100) -> list[dict[str, Any]]:
        res = (
            self.sb.table("mlr_price_history")
            .select("observed_at, price, original_price, discount_pct")
            .eq("product_id", product_id)
            .order("observed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    def last_snapshot_at(self) -> Optional[datetime]:
        rows = (
            self.sb.table("mlr_price_history")
            .select("observed_at")
            .order("observed_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        if not rows:
            return None
        try:
            return datetime.fromisoformat(rows[0]["observed_at"].replace("Z", "+00:00"))
        except ValueError:
            return None

    def data_age_hours(self) -> Optional[float]:
        last = self.last_snapshot_at()
        if last is None:
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600

    def latest_matched_products(self, max_age_hours: int = 48) -> list[Any]:
        """Most recent snapshot per keyword-matched product, as Product objects."""
        from scraper import Product

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        prods = (
            self.sb.table("mlr_products")
            .select("*")
            .neq("matched_keyword", "")
            .gte("last_seen", cutoff)
            .execute()
        ).data or []
        if not prods:
            return []

        by_id = {p["product_id"]: p for p in prods}
        # One snapshot query for all of them, newest first; keep the first hit.
        hist = (
            self.sb.table("mlr_price_history")
            .select("*")
            .in_("product_id", list(by_id))
            .gte("observed_at", cutoff)
            .order("observed_at", desc=True)
            .execute()
        ).data or []

        latest: dict[str, dict[str, Any]] = {}
        for row in hist:
            latest.setdefault(row["product_id"], row)

        out: list[Any] = []
        for pid, h in latest.items():
            p = by_id[pid]
            out.append(
                Product(
                    product_id=pid,
                    title=p["title"],
                    url=p["url"],
                    price=float(h["price"]) if h.get("price") is not None else None,
                    original_price=(
                        float(h["original_price"])
                        if h.get("original_price") is not None else None
                    ),
                    discount_pct=h.get("discount_pct"),
                    currency=h.get("currency") or "ARS",
                    seller=p.get("seller") or "",
                    rating=float(h["rating"]) if h.get("rating") is not None else None,
                    image=p.get("image") or "",
                    badge=h.get("badge") or "",
                    free_shipping=bool(h.get("free_shipping")),
                    matched_keyword=p.get("matched_keyword") or "",
                    matched_label=p.get("matched_label") or "",
                )
            )
        return out

    # ---- generated affiliate links ---------------------------------------

    def get_affiliate_links(self, product_ids: list[str], tag: str) -> dict[str, str]:
        """Cached links for these products, keyed by product_id and scoped by
        tag so switching affiliate accounts never serves the old one's links."""
        if not product_ids:
            return {}
        out: dict[str, str] = {}
        for i in range(0, len(product_ids), 200):
            chunk = product_ids[i : i + 200]
            rows = (
                self.sb.table("mlr_affiliate_links")
                .select("product_id, short_url, full_url")
                .eq("tag", tag)
                .in_("product_id", chunk)
                .execute()
            ).data or []
            for r in rows:
                link = r.get("short_url") or r.get("full_url")
                if link:
                    out[r["product_id"]] = link
        return out

    def save_affiliate_link(
        self, *, product_id: str, product_url: str, short_url: str,
        full_url: str, tag: str,
    ) -> None:
        self.sb.table("mlr_affiliate_links").upsert(
            {
                "product_id": product_id,
                "product_url": product_url,
                "short_url": short_url,
                "full_url": full_url,
                "tag": tag,
                "created_at": _now_iso(),
            },
            on_conflict="product_id",
        ).execute()

    # ---- posts -----------------------------------------------------------

    def recently_posted(self, cooldown_days: int) -> set[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
        res = (
            self.sb.table("mlr_posts")
            .select("product_id")
            .gte("posted_at", cutoff)
            .eq("dry_run", False)
            .execute()
        )
        return {r["product_id"] for r in (res.data or [])}

    def record_post(
        self,
        *,
        product_id: str,
        tweet_id: Optional[str],
        tweet_text: str,
        affiliate_url: str,
        price: Optional[float],
        discount_pct: Optional[int],
        dry_run: bool = False,
    ) -> None:
        self.sb.table("mlr_posts").insert(
            {
                "product_id": product_id,
                "posted_at": _now_iso(),
                "tweet_id": tweet_id,
                "tweet_text": tweet_text,
                "affiliate_url": affiliate_url,
                "price": price,
                "discount_pct": discount_pct,
                "dry_run": dry_run,
            }
        ).execute()

    # ---- reporting -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        def count(table: str, **eq: Any) -> int:
            q = self.sb.table(table).select("*", count="exact").limit(1)
            for k, v in eq.items():
                q = q.eq(k, v)
            return q.execute().count or 0

        last = (
            self.sb.table("mlr_runs")
            .select("started_at")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        return {
            "products": count("mlr_products"),
            "snapshots": count("mlr_price_history"),
            "posts": count("mlr_posts", dry_run=False),
            "runs": count("mlr_runs"),
            "last_run": last[0]["started_at"] if last else "",
        }

    def top_offers(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = (
            self.sb.table("mlr_price_history")
            .select("product_id, price, original_price, discount_pct, observed_at")
            .order("observed_at", desc=True)
            .limit(2000)
            .execute()
        ).data or []

        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            latest.setdefault(r["product_id"], r)

        best = sorted(
            latest.values(), key=lambda r: r.get("discount_pct") or 0, reverse=True
        )[:limit]
        if not best:
            return []

        titles = {
            p["product_id"]: p
            for p in (
                self.sb.table("mlr_products")
                .select("product_id, title, url, matched_label")
                .in_("product_id", [r["product_id"] for r in best])
                .execute()
            ).data or []
        }
        for r in best:
            r.update(
                {k: v for k, v in (titles.get(r["product_id"]) or {}).items() if k != "product_id"}
            )
        return best

    def export_json(self, path: Path | str) -> int:
        rows = (
            self.sb.table("mlr_price_history")
            .select("*")
            .order("observed_at")
            .limit(50_000)
            .execute()
        ).data or []
        Path(path).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(rows)

    def close(self) -> None:
        # supabase-py uses a pooled httpx client; nothing to close explicitly.
        pass
