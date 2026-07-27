"""
Turn scraped products into a ranked list of offers worth posting.

Two stages:
  1. match   — does the title hit one of the configured keywords?
  2. filter  — is the deal actually good enough (discount, price band, rating)?

No historic-price analysis happens here yet, by design. Everything scraped is
snapshotted by the store; "is this the cheapest it's been in 90 days?" becomes
possible once that table has some depth, and slots in as a third stage.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from scraper import Product


def normalize(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace — for tolerant matching."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Keyword:
    term: str
    label: str = ""
    exclude: list[str] = None  # type: ignore[assignment]
    min_discount_pct: Optional[int] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    def __post_init__(self) -> None:
        self.exclude = [normalize(e) for e in (self.exclude or [])]
        if not self.label:
            self.label = self.term.title()

    @classmethod
    def parse(cls, raw: Any) -> "Keyword":
        if isinstance(raw, str):
            return cls(term=raw)
        if isinstance(raw, dict):
            if not raw.get("term"):
                raise ValueError(f"keyword entry is missing 'term': {raw!r}")
            return cls(
                term=raw["term"],
                label=raw.get("label", ""),
                exclude=raw.get("exclude"),
                min_discount_pct=raw.get("min_discount_pct"),
                min_price=raw.get("min_price"),
                max_price=raw.get("max_price"),
            )
        raise ValueError(f"keyword must be a string or object, got {type(raw).__name__}")


def load_keywords(settings: Any) -> list[Keyword]:
    kws = [Keyword.parse(k) for k in (settings.keywords or [])]
    if not kws:
        raise ValueError(
            "No keywords configured. Add them to the \"keywords\" array in "
            "config.json — that list is what the whole pipeline searches for."
        )
    return kws


def _matches(title_n: str, kw: Keyword, mode: str) -> bool:
    term_n = normalize(kw.term)
    if mode == "phrase":
        return term_n in title_n
    # "any_word": every word of the term must be present (order-independent),
    # so "samsung galaxy" also matches "Galaxy A54 Samsung Liberado".
    return all(w in title_n for w in term_n.split())


def match_products(
    products: Iterable[Product], keywords: list[Keyword], settings: Any
) -> list[Product]:
    """Tag each product with the first keyword it matches; drop the rest."""
    mode = settings.keyword_match_mode
    global_exclude = [normalize(e) for e in (settings.global_exclude or [])]
    out: list[Product] = []

    for p in products:
        title_n = normalize(p.title)
        if any(e and e in title_n for e in global_exclude):
            continue
        for kw in keywords:
            if not _matches(title_n, kw, mode):
                continue
            if any(e and e in title_n for e in kw.exclude):
                break  # this keyword rejects it; don't try weaker matches
            p.matched_keyword = kw.term
            p.matched_label = kw.label
            p.extra["keyword"] = kw
            out.append(p)
            break

    return out


def filter_offers(
    products: Iterable[Product], settings: Any, *, exclude_ids: Optional[set[str]] = None
) -> list[Product]:
    """Keep only products that clear the deal thresholds, best discount first."""
    exclude_ids = exclude_ids or set()
    min_rating = float(settings.min_rating or 0)
    kept: list[Product] = []

    for p in products:
        if p.product_id in exclude_ids:
            continue
        if p.price is None:
            continue

        kw: Optional[Keyword] = p.extra.get("keyword")
        min_disc = (kw.min_discount_pct if kw and kw.min_discount_pct is not None
                    else settings.min_discount_pct)
        min_price = (kw.min_price if kw and kw.min_price is not None
                     else settings.min_price)
        max_price = (kw.max_price if kw and kw.max_price is not None
                     else settings.max_price)

        if min_disc and (p.discount_pct or 0) < min_disc:
            continue
        if min_price and p.price < min_price:
            continue
        if max_price and p.price > max_price:
            continue
        # Products with no rating at all are kept — plenty of good deals are new
        # listings. Only an explicitly *low* rating disqualifies.
        if min_rating and p.rating is not None and p.rating < min_rating:
            continue

        kept.append(p)

    kept.sort(key=lambda p: (p.discount_pct or 0, p.savings or 0), reverse=True)
    return kept


def summarize(products: list[Product]) -> dict[str, int]:
    """Count matched offers per keyword label, for logging."""
    counts: dict[str, int] = {}
    for p in products:
        counts[p.matched_label or "?"] = counts.get(p.matched_label or "?", 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
