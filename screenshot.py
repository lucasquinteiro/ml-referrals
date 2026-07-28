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
_WALL_MARKERS = ("account-verification", "/gz/", "bot_challenge")

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


def _search_url(site: str, term: str, page: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower().strip()).strip("-")
    host = site.rstrip("/").replace("://www.", "://listado.")
    if page <= 1:
        return f"{host}/{slug}"
    return f"{host}/{slug}_Desde_{(page - 1) * 50 + 1}_NoIndex_True"


def capture_offer_card(
    product: Any,
    out_path: Path | str,
    *,
    site: str,
    max_pages: int = 12,
    page_hint: Optional[int] = None,
    timeout_ms: int = 45_000,
    source: str = "ofertas",
    search_term: str = "",
) -> Optional[Path]:
    """Screenshot `product`'s card. Returns None when it can't be found.

    Two sources, same card markup:

      "ofertas"  the public offers page — anonymous, no session, safe to run
                 often. Only covers what Mercado Libre flags as discounted.
      "search"   the keyword listings, which is where products ML doesn't
                 surface as "offers" live. Behind the login wall, so this needs
                 the scraping session and should be done in the same sitting as
                 a search ingest rather than as a separate visit.

    Not finding a product is normal, not an error: /ofertas rotates, and search
    results reshuffle.
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

    term = search_term or product.matched_keyword
    if source == "search" and not term:
        log_warn("screenshot: search source needs a keyword; skipping")
        return None

    # Search listings are behind the login wall; /ofertas is not, and stays
    # anonymous so nothing is rate-limited against an account.
    state = None
    if source == "search":
        import auth

        state = auth.storage_state_path(auth.SCRAPING)
        if not state:
            log_warn("screenshot: search source needs `./run login --role scraping`")
            return None

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
                storage_state=str(state) if state else None,
            )
            page = ctx.new_page()

            from lib import mlgate

            gate_account = mlgate.SCRAPING if source == "search" else mlgate.ANON

            for page_no in order:
                url = (_search_url(site, term, page_no) if source == "search"
                       else _offers_url(site, page_no))
                try:
                    mlgate.wait(gate_account, label=f"card {source} p{page_no}")
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if any(m in page.url for m in _WALL_MARKERS):
                        mlgate.trip(gate_account, f"{source} wall")
                        log_warn("screenshot: hit the login wall — the scraping "
                                 "session has expired; re-run `./run login --role scraping`")
                        return None
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
                log_step(f"captured offer card from {'search' if source == 'search' else '/ofertas'} page {page_no}")
                return out_path

            where = f"search '{term}'" if source == "search" else "/ofertas"
            log_warn(f"screenshot: {pid} not found in {max_pages} page(s) of "
                     f"{where}")
            return None
        finally:
            browser.close()
