"""
Render an ML-style offer card by filling an HTML template, then screenshotting
it with headless Chromium.

Why HTML instead of card.py's PIL drawing: the template (templates/offer_card.html)
reproduces Mercado Libre's poly-card look in CSS, so the output reads like a card
off /ofertas — but it's built entirely from data we already hold (title, both
prices, derived discount, installments, shipping, seller) plus the product photo.
Nothing is scraped: no product page, no login wall, no session. The photo is the
only external byte, and its URL comes straight from the API item response.

    from card_html import render
    render(product, "out.png", brand="🦈 Shark Deals")

`product` is any object exposing the scraper.Product attributes (title, price,
original_price, discount_pct, image, installments, free_shipping, seller,
rating, sold, badge, currency, matched_label). Missing/None fields are simply
omitted from the card, so a bare item still renders cleanly.
"""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_TEMPLATE = _HERE / "templates" / "offer_card.html"
_FONT_DIR = _HERE / "templates" / "fonts"

# Montserrat (SIL OFL) stands in for Mercado Libre's Proxima Nova, which is a
# commercial font we can't redistribute. Weights map to the card: 400 body,
# 600 price/discount/shipping, 700 the status pill.
_FONT_WEIGHTS = {400: "Montserrat-400.woff2", 600: "Montserrat-600.woff2",
                 700: "Montserrat-700.woff2"}

# ML blue, the colour of the andes rating star.
_STAR_BLUE = "#3483fa"


def _font_face_css() -> str:
    """@font-face block with each weight inlined as a data: URI (offline-safe)."""
    faces = []
    for weight, name in _FONT_WEIGHTS.items():
        path = _FONT_DIR / name
        if not path.is_file():
            continue
        b64 = base64.b64encode(path.read_bytes()).decode()
        faces.append(
            "@font-face{font-family:'Montserrat';font-style:normal;"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}"
        )
    return "<style>" + "".join(faces) + "</style>" if faces else ""


def _stars_svg(rating: float) -> str:
    """Five andes-style stars (filled to the rounded rating) as inline SVG."""
    full = int(round(rating))
    star = ('<svg viewBox="0 0 24 24" fill="{c}"><path d="M12 17.27l6.18 3.73'
            '-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73'
            'L5.82 21z"/></svg>')
    filled = star.format(c=_STAR_BLUE)
    empty = star.format(c="#d5d9e0")
    return filled * full + empty * (5 - full)


def _fmt_price(value: Optional[float], currency: str = "ARS") -> str:
    if value is None:
        return ""
    symbol = "$" if currency in ("ARS", "MXN", "CLP", "COP", "UYU") else "R$"
    # ML shows "$ 28.999" — dot thousands, space after the symbol.
    return f"{symbol} {int(round(value)):,}".replace(",", ".")


def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _image_data_uri(url: str) -> str:
    """Fetch the photo and inline it as a data: URI.

    Inlining keeps the render deterministic and offline-safe (the screenshot
    can't race a slow CDN), and it means the template needs no network at shot
    time. Falls back to the raw URL if the fetch fails — Chromium can still try.
    """
    if not url:
        return ""
    try:
        if url.startswith(("http://", "https://")):
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310
                data = resp.read()
                mime = resp.headers.get_content_type() or "image/jpeg"
        else:
            data = Path(url).read_bytes()
            mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:  # noqa: BLE001 - let Chromium fetch the raw URL instead
        return url


def _fill(product: Any) -> str:
    """Produce the final HTML: template with every {{TOKEN}} substituted."""
    g = lambda name, default=None: getattr(product, name, default)  # noqa: E731

    currency = g("currency", "ARS") or "ARS"
    price = g("price")
    original = g("original_price")
    discount = g("discount_pct")
    if discount is None and price and original and original > price:
        discount = round((1 - price / original) * 100)

    # --- conditional fragments (empty string when the datum is absent) ---
    badge = g("badge") or ""
    pill = f'<span class="poly-card__pill">{_esc(badge)}</span>' if badge else ""

    was = ""
    if original and price and original != price:
        was = f'<div class="poly-price__was">{_esc(_fmt_price(original, currency))}</div>'

    off = f'<span class="poly-price__off">{int(discount)}% OFF</span>' if discount else ""

    installments = g("installments") or ""
    inst = (f'<div class="poly-price__installments">{_esc(installments)}</div>'
            if installments else "")

    shipping = ('<div class="poly-shipping">Envío gratis</div>'
                if g("free_shipping") else "")

    seller = g("seller") or ""
    seller_html = f'<div class="poly-seller">por {_esc(seller)}</div>' if seller else ""

    rating_html = ""
    rating = g("rating")
    if rating:
        stars = _stars_svg(rating)
        sold = g("sold") or ""
        count = f'<span class="poly-rating__count">{_esc(sold)}</span>' if sold else ""
        rating_html = (
            '<div class="poly-rating">'
            f'<span class="poly-rating__score">{rating:.1f}</span>'
            f'<span class="poly-rating__stars">{stars}</span>{count}</div>'
        )

    tokens = {
        "PILL": pill,
        "IMAGE_URL": _image_data_uri(g("image") or ""),
        "WAS": was,
        "NOW": _esc(_fmt_price(price, currency)),
        "OFF": off,
        "INSTALLMENTS": inst,
        "SHIPPING": shipping,
        "TITLE": _esc(g("title") or ""),
        "SELLER": seller_html,
        "RATING": rating_html,
    }

    tmpl = _TEMPLATE.read_text(encoding="utf-8")
    filled = re.sub(r"\{\{(\w+)\}\}", lambda m: tokens.get(m.group(1), ""), tmpl)
    # Inline the fonts ahead of the markup so they're loaded before layout.
    return _font_face_css() + filled


def render(product: Any, out_path: Path | str, *, brand: str = "", scale: int = 2) -> Path:
    """Fill the template for `product` and screenshot the card to `out_path`.

    `scale` is the device pixel ratio: 2 is crisp on retina timelines at a
    reasonable file size; 3 matches the old screenshot capture exactly.
    """
    from playwright.sync_api import sync_playwright

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_str = _fill(product)

    import os

    launch_kwargs: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
    # Honour a preinstalled Chromium (e.g. CI images that pin the browser out of
    # band) so we don't require `playwright install` at deploy time.
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if exe:
        launch_kwargs["executable_path"] = exe

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_context(device_scale_factor=scale).new_page()
            page.set_content(html_str, wait_until="networkidle")
            # Don't shoot before the inlined font has swapped in.
            page.evaluate("document.fonts.ready")
            # Screenshot the framed node (card + its ML-grey margin), not the
            # bare card, so the result reads as a card sitting on the ML page.
            page.locator("#frame").screenshot(path=str(out_path))
        finally:
            browser.close()
    return out_path


# --------------------------------------------------------------------------
# Preview: `python card_html.py [out.png]` renders a mock card so the template
# can be eyeballed without a scrape or an API call.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from types import SimpleNamespace

    # A stand-in product photo (grey tile) so the preview needs no network.
    def _placeholder_photo() -> str:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (500, 500), (245, 245, 245))
        d = ImageDraw.Draw(img)
        d.rectangle([120, 150, 380, 360], fill=(214, 219, 226))
        d.text((180, 245), "PRODUCTO", fill=(120, 128, 140))
        tmp = Path("/tmp/_ph.png")
        img.save(tmp)
        return str(tmp)

    mock = SimpleNamespace(
        title="Creatina Monohidrato ENA Pure 300g Sin Sabor",
        price=18999.0,
        original_price=28999.0,
        discount_pct=None,           # left None on purpose → derived from prices
        currency="ARS",
        image=_placeholder_photo(),
        installments="en 12x $ 1.583 sin interés",
        free_shipping=True,
        seller="ENA SPORT OFICIAL",
        rating=4.8,
        sold="+500 vendidos",
        badge="MÁS VENDIDO",
        matched_label="Suplementos",
    )
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/offer_card_preview.png"
    render(mock, out)
    print(f"wrote {out}")
