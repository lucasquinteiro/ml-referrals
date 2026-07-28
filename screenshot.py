"""
Screenshot a product's real Mercado Libre offer card.

Deal accounts post an image that looks like Mercado Libre because it *is*
Mercado Libre — their card, their typography, their discount pill and
strike-through price. This grabs exactly that.

It captures the card from `/ofertas`, not from the product page. That choice is
the whole point:

  * `/ofertas` renders for anonymous visitors, so nothing is rate-limited
    against an account. Product pages need a login, and driving them repeatedly
    got the affiliate account walled within three requests during testing.
  * A logged-in page also renders the account holder's name, delivery address
    and cart in the header — all of which would end up published.

So: no session, no personal data, same public page the ingest already reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from lib.log import log_step, log_warn

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CARD_SELECTOR = "div.andes-card.poly-card"
TITLE_SELECTOR = "a.poly-component__title"

# Fixed-position overlays float above the page and get baked into an element
# screenshot when they happen to sit over the card. Hidden rather than
# dismissed: clicking "Aceptar cookies" would record a consent decision on the
# account holder's behalf, which isn't ours to give.
_HIDE_OVERLAYS_CSS = """
.cookie-consent-banner-opt-out,
[class*="cookie-consent"],
.andes-snackbar,
.nav-bottom-bar,
[class*="onboarding"],
[class*="modal-backdrop"],
[class*="andes-modal"] { display: none !important; visibility: hidden !important; }
"""

# 3x gives a ~850px-wide capture of a ~284px card — sharp on retina timelines
# without producing a file big enough to bother X's upload limit.
SCALE = 3


def _offers_url(site: str, page: int) -> str:
    base = f"{site.rstrip('/')}/ofertas"
    return base if page <= 1 else f"{base}?page={page}"


def capture_offer_card(
    product: Any,
    out_path: Path | str,
    *,
    site: str,
    max_pages: int = 12,
    page_hint: Optional[int] = None,
    timeout_ms: int = 45_000,
) -> Optional[Path]:
    """Find `product` on /ofertas and screenshot its card. None if not found.

    Not finding it is normal and not an error: /ofertas rotates, and an offer
    that has dropped off is one whose price we'd be quoting stale anyway.
    """
    from playwright.sync_api import sync_playwright

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # The stored id is normalised (MLA69985783); hrefs may carry MLA-69985783.
    pid = product.product_id.upper()
    loose = re.compile(re.escape(pid[:3]) + r"-?" + re.escape(pid[3:]), re.I)

    # Try the page it was scraped from first — usually a single load.
    order = list(range(1, max_pages + 1))
    if page_hint and 1 <= page_hint <= max_pages:
        order.remove(page_hint)
        order.insert(0, page_hint)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            ctx = browser.new_context(
                locale="es-AR",
                viewport={"width": 1440, "height": 1000},
                device_scale_factor=SCALE,
                user_agent=UA,  # without a real UA, ML serves not-found
            )
            page = ctx.new_page()

            for page_no in order:
                try:
                    page.goto(_offers_url(site, page_no),
                              wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_selector(CARD_SELECTOR, timeout=25_000)
                    page.add_style_tag(content=_HIDE_OVERLAYS_CSS)
                except Exception as e:  # noqa: BLE001 - try the next page
                    log_warn(f"screenshot: page {page_no} failed "
                             f"({type(e).__name__}); trying next")
                    continue

                index = page.evaluate(
                    """([sel, titleSel, pattern]) => {
                        const re = new RegExp(pattern, 'i');
                        const cards = document.querySelectorAll(sel);
                        for (let i = 0; i < cards.length; i++) {
                            const a = cards[i].querySelector(titleSel);
                            if (a && re.test(a.href)) return i;
                        }
                        return -1;
                    }""",
                    [CARD_SELECTOR, TITLE_SELECTOR, loose.pattern],
                )
                if index is None or index < 0:
                    continue

                card = page.locator(CARD_SELECTOR).nth(index)
                # Images are lazy-loaded; the card must be on screen and settled
                # or the screenshot catches an empty photo area.
                card.scroll_into_view_if_needed()
                page.wait_for_timeout(1500)
                card.screenshot(path=str(out_path))
                log_step(f"captured offer card from /ofertas page {page_no}")
                return out_path

            log_warn(f"screenshot: {pid} not found in {max_pages} page(s) of "
                     "/ofertas — it has probably rotated off")
            return None
        finally:
            browser.close()
