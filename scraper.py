"""
MercadoLibre offers scraper.

Source is the public /ofertas page rather than /listado search. That is a
deliberate choice: as of 2026-07 MercadoLibre Argentina puts search listings
behind a login wall ("Para continuar, ingresá a tu cuenta") and a JS bot
challenge, while /ofertas renders for anonymous visitors. /ofertas is also a
better fit — it is literally the discounted catalogue (~10k products), paginated
at ~45 cards a page, with the list price, sale price and discount already on the
card. We crawl N pages and match titles against the configured keywords locally
(the page's own ?q= parameter is silently ignored by MercadoLibre).

No official API is used. Rendering happens in a real headless Chromium, so the
page's own JS runs exactly as it does for a normal visitor.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from lib.log import log_err, log_step, log_warn

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Matches the id in /p/MLA54630200 (catalog), /up/MLAU4188729372 (universal
# product) and MLA-1234567890-slug (plain item listing).
_ID_RE = re.compile(r"/(?:u?p/)?(ML[A-Z]{1,2}-?\d{6,})", re.I)
_WALL_MARKERS = ("account-verification", "/gz/", "bot_challenge")


@dataclass
class Product:
    """One offer card, as scraped."""

    product_id: str
    title: str
    url: str  # canonical, tracking params stripped
    price: Optional[float]
    original_price: Optional[float]
    discount_pct: Optional[int]
    currency: str = "ARS"
    seller: str = ""
    rating: Optional[float] = None
    sold: str = ""
    image: str = ""
    badge: str = ""  # "OFERTA DEL DÍA", "OFERTA RELÁMPAGO", ...
    installments: str = ""
    free_shipping: bool = False
    # Filled in by offers.py once the product is matched to a keyword.
    matched_keyword: str = ""
    matched_label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def savings(self) -> Optional[float]:
        if self.price is None or self.original_price is None:
            return None
        return round(self.original_price - self.price, 2)


# JS that runs inside the page and pulls every card out in one round trip.
# Doing it here rather than through many Playwright locator calls keeps a
# 45-card page to a single IPC hop.
_EXTRACT_JS = r"""
() => {
  const num = (el) => {
    if (!el) return null;
    // aria-label is the reliable source: "Antes: 45999 pesos argentinos",
    // "37829 pesos argentinos con 50 centavos". The visible text uses locale
    // separators that differ per site.
    const al = el.getAttribute('aria-label') || '';
    const m = al.replace(/^[^\d]*/, '').match(/(\d+)(?:\D+(\d{1,2})\s*centavos)?/);
    if (m) return parseFloat(m[2] ? `${m[1]}.${m[2]}` : m[1]);
    const f = el.querySelector('.andes-money-amount__fraction');
    if (!f) return null;
    const c = el.querySelector('.andes-money-amount__cents');
    const whole = f.textContent.replace(/\D/g, '');
    return whole ? parseFloat(c ? `${whole}.${c.textContent.replace(/\D/g,'')}` : whole) : null;
  };
  const txt = (root, sel) => {
    const el = root.querySelector(sel);
    return el ? el.textContent.trim().replace(/\s+/g, ' ') : '';
  };

  const cards = document.querySelectorAll('div.andes-card.poly-card, li.andes-card');
  return Array.from(cards).map((card) => {
    const a = card.querySelector('a.poly-component__title');
    if (!a) return null;

    const pill = txt(card, '.poly-price__label .polylabel-pill');
    const discount = (pill.match(/(\d+)\s*%/) || [])[1];

    const review = txt(card, '.poly-component__review-compacted');
    const rating = (review.match(/^([\d.,]+)/) || [])[1];
    const sold = (review.match(/\|\s*(.+)$/) || [])[1] || '';

    const img = card.querySelector('img.poly-component__picture');
    const shipping = txt(card, '.poly-component__shipping');

    return {
      title: a.textContent.trim().replace(/\s+/g, ' '),
      url: a.href,
      price: num(card.querySelector('.poly-price__current .andes-money-amount')),
      original_price: num(card.querySelector('s.andes-money-amount--previous')),
      discount_pct: discount ? parseInt(discount, 10) : null,
      seller: txt(card, '.poly-component__seller'),
      rating: rating ? parseFloat(rating.replace(',', '.')) : null,
      sold: sold,
      image: img ? (img.getAttribute('src') || '') : '',
      badge: txt(card, '.poly-component__poly-label span'),
      installments: txt(card, '.poly-price__installments'),
      free_shipping: /gratis/i.test(shipping),
    };
  }).filter(Boolean);
}
"""


class BlockedError(RuntimeError):
    """MercadoLibre served a login wall or bot challenge instead of the page."""


def _canonical_url(url: str) -> str:
    """Strip tracking/session query params and fragments off a card link."""
    return url.split("#", 1)[0].split("?", 1)[0]


def _product_id(url: str, title: str) -> str:
    m = _ID_RE.search(url)
    if m:
        return m.group(1).upper().replace("-", "")
    # Fall back to a stable hash of the canonical URL so nothing is dropped.
    import hashlib

    return "URL" + hashlib.sha1(_canonical_url(url).encode()).hexdigest()[:16]


def _to_product(raw: dict[str, Any]) -> Optional[Product]:
    url = raw.get("url") or ""
    title = (raw.get("title") or "").strip()
    if not url or not title:
        return None

    canonical = _canonical_url(url)
    discount = raw.get("discount_pct")
    price = raw.get("price")
    original = raw.get("original_price")

    # Derive the discount when the card shows both prices but no % pill.
    if discount is None and price and original and original > price:
        discount = round((1 - price / original) * 100)

    return Product(
        product_id=_product_id(canonical, title),
        title=title,
        url=canonical,
        price=price,
        original_price=original,
        discount_pct=int(discount) if discount is not None else None,
        seller=raw.get("seller") or "",
        rating=raw.get("rating"),
        sold=raw.get("sold") or "",
        image=raw.get("image") or "",
        badge=raw.get("badge") or "",
        installments=raw.get("installments") or "",
        free_shipping=bool(raw.get("free_shipping")),
    )


class MercadoLibreScraper:
    """Crawls /ofertas with a real headless browser.

    Usage:
        with MercadoLibreScraper(site, headless=True) as s:
            products = s.scrape_offers(pages=10)
    """

    def __init__(
        self,
        site: str,
        *,
        headless: bool = True,
        delay_sec: float = 2.5,
        timeout_ms: int = 60_000,
    ) -> None:
        self.site = site.rstrip("/")
        self.headless = headless
        self.delay_sec = delay_sec
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._ctx = None

    # ---- context management ---------------------------------------------

    def __enter__(self) -> "MercadoLibreScraper":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._ctx = self._browser.new_context(
            locale="es-AR",
            user_agent=UA,
            viewport={"width": 1440, "height": 1000},
        )
        # Images/fonts/media are pure bandwidth here — the thumbnail URL is in
        # the DOM regardless of whether the bytes are fetched.
        self._ctx.route(
            re.compile(r"\.(png|jpe?g|webp|avif|gif|svg|woff2?|ttf|mp4)(\?|$)", re.I),
            lambda route: route.abort(),
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---- scraping --------------------------------------------------------

    def _offers_url(self, page: int) -> str:
        base = f"{self.site}/ofertas"
        return base if page <= 1 else f"{base}?page={page}"

    def _scrape_page(self, page_no: int) -> list[Product]:
        assert self._ctx is not None, "use the scraper as a context manager"
        pg = self._ctx.new_page()
        try:
            url = self._offers_url(page_no)
            pg.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                pg.wait_for_selector("a.poly-component__title", timeout=25_000)
            except Exception:  # noqa: BLE001 - inspect where we actually landed
                landed = pg.url
                if any(m in landed for m in _WALL_MARKERS):
                    raise BlockedError(
                        f"MercadoLibre served a wall instead of {url} (landed on "
                        f"{landed}). If this persists the /ofertas page may now "
                        f"require a login too."
                    )
                log_warn(f"page {page_no}: no offer cards appeared ({landed})")
                return []

            raw = pg.evaluate(_EXTRACT_JS)
            products = [p for p in (_to_product(r) for r in raw) if p]
            log_step(f"page {page_no}: {len(products)} products")
            return products
        finally:
            pg.close()

    def scrape_offers(self, pages: int = 10) -> list[Product]:
        """Crawl `pages` pages of /ofertas, de-duplicated by product id."""
        seen: dict[str, Product] = {}
        for n in range(1, pages + 1):
            try:
                batch = self._scrape_page(n)
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad page shouldn't kill the run
                log_err(f"page {n} failed: {type(e).__name__}: {e}")
                continue

            if not batch:
                # /ofertas ran out of pages (or markup changed) — stop early.
                log_step(f"page {n} empty; stopping pagination")
                break

            new = 0
            for p in batch:
                if p.product_id not in seen:
                    seen[p.product_id] = p
                    new += 1
            if new == 0:
                log_step(f"page {n} was all duplicates; stopping pagination")
                break

            if n < pages:
                time.sleep(self.delay_sec)

        return list(seen.values())


def iter_products(products: list[Product]) -> Iterator[Product]:
    return iter(products)
