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
/* The "Otra opción de compra" secondary buy-box makes the card long and
   confusing (it shows a worse second price). Drop it so the card is just the
   headline offer: photo, title, price. */
.poly-component__buy-box { display: none !important; }
"""

# 3x gives a ~850px-wide capture of a ~284px card — sharp on retina timelines
# without producing a file big enough to bother X's upload limit.
SCALE = 3


def _offers_url(site: str, page: int, category: Optional[str] = None) -> str:
    base = f"{site.rstrip('/')}/ofertas"
    params = []
    if category:
        params.append(f"category={category}")
    if page > 1:
        params.append(f"page={page}")
    return base + (f"?{'&'.join(params)}" if params else "")


def _search_url(site: str, term: str, page: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower().strip()).strip("-")
    host = site.rstrip("/").replace("://www.", "://listado.")
    if page <= 1:
        return f"{host}/{slug}"
    return f"{host}/{slug}_Desde_{(page - 1) * 50 + 1}_NoIndex_True"


# The product-detail header block: gallery + title + price + buy box. This is
# the "desktop layout" — captured straight from the product page, not the
# compact listing card. Tried in order; first match wins.
_PDP_SELECTORS = [
    ".ui-pdp-container",   # the product content wrapper (starts below the nav)
    "main",                # fallback
]

# Nav + anything carrying the logged-in user's identity. Hidden before the
# shot so a product-page capture never leaks the account holder's name,
# address or cart.
_PDP_HIDE_CSS = """
.nav-header, header.nav-header, .andes-navbar, nav.nav-menu,
[class*="nav-header"], [class*="navbar"],
.ui-pdp-notification, .andes-snackbar,
[class*="cookie-consent"] { display:none !important; visibility:hidden !important; }
"""


def capture_product_page(
    product: Any,
    out_path: Path | str,
    *,
    site: str,
    timeout_ms: int = 45_000,
) -> Optional[Path]:
    """Screenshot the product's *desktop* detail page — one page load.

    This is the desktop layout (gallery, title, price, buy box), not the compact
    listing card. Product pages sit behind the login wall, so it uses the
    affiliate session — deliberately, not the burner: at post time this is a
    single, human-paced load (~8/day), gated through mlgate's affiliate budget,
    which is the gentle usage a real account survives. The nav/identity chrome
    is hidden first so nothing personal is captured.

    Note: driving PDPs *repeatedly* has walled the affiliate account fast in the
    past, so keep this to one shot per post and prefer a persistent profile.
    """
    import auth
    from lib import mlgate

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    role = auth.AFFILIATE
    if not auth.has_session(role):
        log_warn("screenshot: product pages need `./run login --role affiliate`")
        return None

    # BrowserSession picks the persistent profile over a frozen snapshot when one
    # exists — preferred here, since a live profile keeps ML's rotating tokens
    # fresh and PDPs are wall-prone.
    try:
        with auth.BrowserSession(
            role, headless=True, user_agent=UA,
            viewport={"width": 1440, "height": 1600}, device_scale_factor=2,
        ) as ctx:
            page = ctx.new_page()
            mlgate.wait(mlgate.AFFILIATE, label="pdp screenshot")
            page.goto(product.url, wait_until="domcontentloaded", timeout=timeout_ms)
            if any(m in page.url for m in _WALL_MARKERS):
                mlgate.trip(mlgate.AFFILIATE, "pdp wall")
                log_warn("screenshot: product page hit the login wall — the "
                         "affiliate session expired; re-run "
                         "`./run login --role affiliate`")
                return None
            page.wait_for_timeout(2500)
            page.add_style_tag(content=_PDP_HIDE_CSS)
            page.wait_for_timeout(400)

            # Clip the TOP of the product container — the hero row: gallery,
            # title, price, buy box. That's the recognisable desktop layout, and
            # starting at the container (well below the nav) structurally leaves
            # out the header where the account holder's name and address sit.
            box = None
            for sel in _PDP_SELECTORS:
                el = page.locator(sel).first
                try:
                    if el.count():
                        b = el.bounding_box()
                        if b and b["height"] > 300 and b["width"] > 400:
                            box = b
                            break
                except Exception:  # noqa: BLE001
                    continue

            if box:
                clip = {
                    "x": max(0, box["x"]),
                    "y": max(0, box["y"]),
                    "width": box["width"],
                    "height": min(920, box["height"]),  # hero row only
                }
            else:
                # Fallback: a fixed band below where the nav would be.
                clip = {"x": 120, "y": 150, "width": 1200, "height": 900}

            page.screenshot(path=str(out_path), clip=clip)
            log_step("captured desktop product page")
            return out_path
    except Exception as e:  # noqa: BLE001 - never let a capture crash a post
        log_warn(f"pdp screenshot failed ({type(e).__name__}: {e})")
        return None


def capture_card_on_page(
    page: Any, product: Any, out_path: Path | str, *, settle_ms: int = 1500,
) -> Optional[Path]:
    """Screenshot just `product`'s card on an already-loaded listing page.

    The page must already be navigated to an /ofertas (or search) listing whose
    cards have rendered (and, ideally, overlays hidden via _HIDE_OVERLAYS_CSS).
    This only locates the one card whose title href carries the product id and
    screenshots that element — nothing else on the page. Returns the path, or
    None when the card isn't present on this page.

    Isolated so both the post-time capture (which navigates, then calls this)
    and the ingest-time batch capture (which loads a page once and calls this
    for every wanted card on it) share the exact same "keep only our card" grab.
    """
    pid = product.product_id.upper()
    # The stored id is normalised (MLA69985783); hrefs may carry MLA-69985783.
    loose = re.compile(re.escape(pid[:3]) + r"-?" + re.escape(pid[3:]), re.I)

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
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    card = page.locator(CARD_SELECTOR).nth(index)
    # Images are lazy-loaded; the card must be on screen and settled or the
    # screenshot catches an empty photo area.
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(settle_ms)
    card.screenshot(path=str(out_path))
    return out_path


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
    categories: Optional[list[str]] = None,
    session_role: Optional[str] = None,
) -> Optional[Path]:
    """Screenshot `product`'s card. Returns None when it can't be found.

    Two sources, same card markup:

      "ofertas"  the public offers page — anonymous, no session, safe to run
                 often. Only covers what Mercado Libre flags as discounted. When
                 `categories` is given (the same list ingest crawled), each
                 category's /ofertas?category= is tried in turn — a product
                 scraped from a niche category may not surface within
                 `max_pages` of the plain, unfiltered /ofertas, but it's
                 guaranteed to be near the front of the category it came from.
      "search"   the keyword listings, which is where products ML doesn't
                 surface as "offers" live. Behind the login wall, so this needs
                 a logged-in session — `session_role` picks which one (default
                 `auth.SCRAPING`, the burner). Callers that want to keep the
                 burner out of the image path entirely should pass
                 `session_role=auth.AFFILIATE` explicitly and treat it as a
                 last resort, not the default path.

    Not finding a product is normal, not an error: /ofertas rotates, and search
    results reshuffle.
    """
    from playwright.sync_api import sync_playwright

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pid = product.product_id.upper()

    # Try the page it was scraped from first — usually a single load.
    order = list(range(1, max_pages + 1))
    if page_hint and 1 <= page_hint <= max_pages:
        order.remove(page_hint)
        order.insert(0, page_hint)

    term = search_term or product.matched_keyword
    if source == "search" and not term:
        log_warn("screenshot: search source needs a keyword; skipping")
        return None

    import auth

    # Search listings are behind the login wall; /ofertas is not, and stays
    # anonymous so nothing is rate-limited against an account. The role behind
    # "search" is caller-chosen (session_role), defaulting to the burner only
    # for backward compatibility — callers that care which account takes the
    # risk should always pass session_role explicitly.
    role = (session_role or auth.SCRAPING) if source == "search" else None
    if source == "search" and not auth.has_session(role):
        log_warn(f"screenshot: search source needs `./run login --role {role}`")
        return None

    # Candidate category filters to try, in order: no filter first (fastest
    # when the product is a broadly-featured deal), then each configured
    # category (dedup, category filters only apply to source="ofertas").
    cat_attempts: list[Optional[str]] = [None]
    if source == "ofertas" and categories:
        for c in categories:
            if c and c not in cat_attempts:
                cat_attempts.append(c)

    with auth.BrowserSession(
        role, viewport={"width": 1440, "height": 1000},
        device_scale_factor=SCALE, user_agent=UA,
    ) as ctx:
        try:
            page = ctx.new_page()

            from lib import mlgate

            # auth's role constants ("scraping"/"affiliate") are the same
            # strings mlgate's account constants use, so the resolved role
            # doubles as the gate account directly.
            gate_account = role if source == "search" else mlgate.ANON

            for cat in cat_attempts:
                for page_no in order:
                    url = (_search_url(site, term, page_no) if source == "search"
                           else _offers_url(site, page_no, cat))
                    label = f"card {source}{f'[{cat}]' if cat else ''} p{page_no}"
                    try:
                        mlgate.wait(gate_account, label=label)
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        if any(m in page.url for m in _WALL_MARKERS):
                            mlgate.trip(gate_account, f"{source} wall")
                            log_warn(f"screenshot: hit the login wall — the {gate_account} "
                                     f"session has expired; re-run `./run login --role {gate_account}`")
                            return None
                        page.wait_for_selector(CARD_SELECTOR, timeout=25_000)
                        page.add_style_tag(content=_HIDE_OVERLAYS_CSS)
                    except Exception as e:  # noqa: BLE001 - try the next page
                        log_warn(f"screenshot: page {page_no} failed "
                                 f"({type(e).__name__}); trying next")
                        continue

                    if capture_card_on_page(page, product, out_path) is None:
                        continue

                    where = (f"search '{term}'" if source == "search"
                             else f"/ofertas{f'?category={cat}' if cat else ''}")
                    log_step(f"captured offer card from {where} page {page_no}")
                    return out_path

            where = f"search '{term}'" if source == "search" else "/ofertas"
            tried = f" ({len(cat_attempts)} categor{'y' if len(cat_attempts)==1 else 'ies'} tried)" if len(cat_attempts) > 1 else ""
            log_warn(f"screenshot: {pid} not found in {max_pages} page(s) of "
                     f"{where}{tried}")
            return None
        except Exception as e:  # noqa: BLE001 - never let a capture crash a post
            log_warn(f"screenshot failed ({type(e).__name__}: {e})")
            return None
