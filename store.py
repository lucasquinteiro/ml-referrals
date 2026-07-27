"""
SQLite-backed store: product catalogue, price history, and posted-tweet log.

Three tables:
  products      one row per product we've ever seen (latest metadata wins)
  price_history one row per (product, run) price observation — append-only
  posts         one row per tweet we sent, for dedupe + a record of the link

Deliberately no analysis on top of price_history yet: this run just records
snapshots so that a later "cheapest in 90 days" check has data to stand on.

`SupabaseStore` in supabase_store.py exposes the same method surface, so run.py
doesn't care which backend it's talking to.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id      TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    image           TEXT,
    seller          TEXT,
    matched_keyword TEXT,
    matched_label   TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    price          REAL,
    original_price REAL,
    discount_pct   INTEGER,
    currency       TEXT DEFAULT 'ARS',
    badge          TEXT,
    free_shipping  INTEGER DEFAULT 0,
    rating         REAL,
    run_id         INTEGER,
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);
CREATE INDEX IF NOT EXISTS idx_price_history_product
    ON price_history(product_id, observed_at);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    TEXT NOT NULL,
    posted_at     TEXT NOT NULL,
    tweet_id      TEXT,
    tweet_text    TEXT,
    affiliate_url TEXT,
    price         REAL,
    discount_pct  INTEGER,
    dry_run       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_product ON posts(product_id, posted_at);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    kind           TEXT,
    products_seen  INTEGER DEFAULT 0,
    offers_matched INTEGER DEFAULT 0,
    note           TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- runs ------------------------------------------------------------

    def start_run(self, kind: str, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, kind, note) VALUES (?, ?, ?)",
            (_now_iso(), kind, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, products_seen: int, offers_matched: int) -> None:
        self.conn.execute(
            "UPDATE runs SET products_seen = ?, offers_matched = ? WHERE id = ?",
            (products_seen, offers_matched, run_id),
        )
        self.conn.commit()

    # ---- products + price snapshots -------------------------------------

    def record_snapshot(self, product: Any, run_id: Optional[int] = None) -> None:
        """Upsert the product and append one price observation."""
        now = _now_iso()
        self.conn.execute(
            """
            INSERT INTO products (product_id, title, url, image, seller,
                                  matched_keyword, matched_label, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                title = excluded.title,
                url = excluded.url,
                image = excluded.image,
                seller = excluded.seller,
                matched_keyword = COALESCE(NULLIF(excluded.matched_keyword, ''),
                                           products.matched_keyword),
                matched_label = COALESCE(NULLIF(excluded.matched_label, ''),
                                         products.matched_label),
                last_seen = excluded.last_seen
            """,
            (
                product.product_id, product.title, product.url, product.image,
                product.seller, product.matched_keyword, product.matched_label,
                now, now,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO price_history (product_id, observed_at, price, original_price,
                                       discount_pct, currency, badge, free_shipping,
                                       rating, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product.product_id, now, product.price, product.original_price,
                product.discount_pct, product.currency, product.badge,
                int(product.free_shipping), product.rating, run_id,
            ),
        )

    def record_snapshots(self, products: Iterable[Any], run_id: Optional[int] = None) -> int:
        n = 0
        for p in products:
            self.record_snapshot(p, run_id)
            n += 1
        self.conn.commit()
        return n

    def price_history(self, product_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT observed_at, price, original_price, discount_pct
               FROM price_history WHERE product_id = ?
               ORDER BY observed_at DESC LIMIT ?""",
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_snapshot_at(self) -> Optional[datetime]:
        """When the most recent price snapshot was taken, or None if empty."""
        row = self.conn.execute("SELECT MAX(observed_at) FROM price_history").fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    def data_age_hours(self) -> Optional[float]:
        """Hours since the last snapshot; None when nothing has been scraped."""
        last = self.last_snapshot_at()
        if last is None:
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600

    def latest_matched_products(self, max_age_hours: int = 48) -> list[Any]:
        """Rebuild Product objects from the most recent snapshot of each product.

        Lets `./run post` work off the last ingest instead of re-scraping.
        Snapshots older than `max_age_hours` are skipped — a stale price is
        worse than no post, since the offer has probably expired.
        """
        from scraper import Product

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        rows = self.conn.execute(
            """
            SELECT p.product_id, p.title, p.url, p.image, p.seller,
                   p.matched_keyword, p.matched_label,
                   h.price, h.original_price, h.discount_pct, h.currency,
                   h.badge, h.free_shipping, h.rating
            FROM products p
            JOIN price_history h ON h.id = (
                SELECT id FROM price_history
                WHERE product_id = p.product_id
                ORDER BY observed_at DESC, id DESC LIMIT 1
            )
            WHERE h.observed_at >= ? AND p.matched_keyword != ''
            """,
            (cutoff,),
        ).fetchall()

        out: list[Any] = []
        for r in rows:
            out.append(
                Product(
                    product_id=r["product_id"],
                    title=r["title"],
                    url=r["url"],
                    price=r["price"],
                    original_price=r["original_price"],
                    discount_pct=r["discount_pct"],
                    currency=r["currency"] or "ARS",
                    seller=r["seller"] or "",
                    rating=r["rating"],
                    image=r["image"] or "",
                    badge=r["badge"] or "",
                    free_shipping=bool(r["free_shipping"]),
                    matched_keyword=r["matched_keyword"] or "",
                    matched_label=r["matched_label"] or "",
                )
            )
        return out

    # ---- posts -----------------------------------------------------------

    def recently_posted(self, cooldown_days: int) -> set[str]:
        """Product ids posted within the cooldown window (dry runs excluded)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
        rows = self.conn.execute(
            "SELECT DISTINCT product_id FROM posts WHERE posted_at >= ? AND dry_run = 0",
            (cutoff,),
        ).fetchall()
        return {r["product_id"] for r in rows}

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
        self.conn.execute(
            """INSERT INTO posts (product_id, posted_at, tweet_id, tweet_text,
                                  affiliate_url, price, discount_pct, dry_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                product_id, _now_iso(), tweet_id, tweet_text, affiliate_url,
                price, discount_pct, int(dry_run),
            ),
        )
        self.conn.commit()

    # ---- reporting -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "products": q("SELECT COUNT(*) FROM products"),
            "snapshots": q("SELECT COUNT(*) FROM price_history"),
            "posts": q("SELECT COUNT(*) FROM posts WHERE dry_run = 0"),
            "runs": q("SELECT COUNT(*) FROM runs"),
            "last_run": q("SELECT COALESCE(MAX(started_at), '') FROM runs"),
        }

    def top_offers(self, limit: int = 20) -> list[dict[str, Any]]:
        """Latest snapshot per product, best discount first."""
        rows = self.conn.execute(
            """
            SELECT p.product_id, p.title, p.url, p.matched_label,
                   h.price, h.original_price, h.discount_pct, h.observed_at
            FROM products p
            JOIN price_history h ON h.id = (
                SELECT id FROM price_history
                WHERE product_id = p.product_id
                ORDER BY observed_at DESC, id DESC LIMIT 1
            )
            ORDER BY h.discount_pct DESC NULLS LAST
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_json(self, path: Path | str) -> int:
        rows = self.conn.execute(
            """SELECT p.product_id, p.title, p.url, h.observed_at, h.price,
                      h.original_price, h.discount_pct
               FROM price_history h JOIN products p USING (product_id)
               ORDER BY h.observed_at"""
        ).fetchall()
        data = [dict(r) for r in rows]
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(data)

    def close(self) -> None:
        self.conn.close()
