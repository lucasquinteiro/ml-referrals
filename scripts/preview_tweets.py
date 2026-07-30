"""Preview the tweet templates in the console — no store, no network.

Renders every template in tweets._TEMPLATES against a sample offer so you can
iterate on the copy format quickly:

    python scripts/preview_tweets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tweets as tw
from config import load_settings
from scraper import Product

SAMPLE = Product(
    product_id="SAMPLE",
    title="Auriculares Inalámbricos Sony WH-1000XM5 con Cancelación de Ruido",
    url="https://articulo.mercadolibre.com.ar/MLA-sample",
    price=289999.0,
    original_price=499999.0,
    discount_pct=42,
    currency="ARS",
    matched_label="Tecnología",
    matched_keyword="auriculares",
    badge="OFERTA DEL DÍA",
    free_shipping=True,
)
LINK = "https://mercadolibre.com.ar/sec/abcd123"


def main() -> int:
    settings = load_settings()
    tail = tw._tail(settings)
    for i, template in enumerate(tw._TEMPLATES):
        fields = {
            "discount": SAMPLE.discount_pct or 0,
            "was": tw._fmt_price(SAMPLE.original_price, SAMPLE.currency),
            "now": tw._fmt_price(SAMPLE.price, SAMPLE.currency),
            "saved": tw._fmt_price(SAMPLE.savings, SAMPLE.currency),
            "label": SAMPLE.matched_label,
        }
        text = template.format(title=SAMPLE.title, link=LINK, **fields) + tail
        length = tw.tweet_length(text, LINK)
        print(f"\n\033[1m─── template {i} · {length}/{tw.TWEET_LIMIT} chars\033[0m")
        print(text)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
