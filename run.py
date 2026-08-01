#!/usr/bin/env python3
"""
ml-referrals — main entrypoint.

Three steps: scrape, check what would go out, publish it.

    ./run ingest            scrape MercadoLibre, match against the keywords in
                            config.json, snapshot every price
    ./run simulate          show exactly what the next post would publish —
                            writes nothing, posts nothing
    ./run post              publish that one tweet

    ./run offers            browse the whole queue, not just the next one
    ./run db                query the store
    ./run report            summary of what's in the store
    ./run login             save a session, unlocking --source search

Typical local test drive:

    ./run ingest                    # scrape + record price snapshots
    ./run simulate                  # exactly what would be posted
    ./run post                      # publish it

    ./run db --name offers          # best current discounts
    ./run set-affiliate <link>      # configure your affiliate tag from a
                                    # link generated in the ML dashboard

Posting is always ONE tweet per run. A burst of affiliate links reads as spam,
and one at a time keeps every publish reviewable.

`simulate`, `offers` and `post` all refuse to run on data older than
config.max_data_age_hours: a tweet built from an expired price is worse than
no tweet.

Deployed on a droplet, systemd timers run `ingest`, `post` and `session-check`
on gentle schedules — see deploy/ and DEPLOY.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Optional

import config as cfg


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Find MercadoLibre offers, track prices, tweet affiliate links",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Scrape offers and record price snapshots")
    ing.add_argument("--pages", type=int, default=None,
                     help="Pages of /ofertas to crawl (default: config.pages_per_run)")
    ing.add_argument("--dry-run", action="store_true",
                     help="Scrape and match, but write nothing to the store")
    ing.add_argument("--headed", action="store_true",
                     help="Show the browser window (debugging)")
    ing.add_argument("--keyword", action="append", default=None,
                     help="Override config keywords (repeatable)")
    ing.add_argument("--all", action="store_true",
                     help="Snapshot every scraped product, not just keyword matches")
    ing.add_argument("--source", choices=["ofertas", "search"], default="ofertas",
                     help="ofertas (default, no login) or search (needs ./run login)")
    ing.add_argument("--category", action="append", default=None, metavar="MLA_ID",
                     help="Crawl only this /ofertas category (repeatable), e.g. "
                          "--category MLA1051. Overrides config.categories. Handy "
                          "for testing a single category from the console.")
    ing.add_argument("--no-slack", action="store_true",
                     help="Skip the Slack summary even if SLACK_WEBHOOK_URL is set")
    ing.add_argument("--no-capture-cards", action="store_true",
                     help="Skip screenshotting each keeper's /ofertas card at "
                          "ingest time (the cached card the post later uses)")
    ing.add_argument("--capture-limit", type=int, default=None, metavar="N",
                     help="Max offer cards to screenshot this run "
                          "(default: config max_cards_per_ingest)")
    ing.add_argument("--start-page", type=int, default=1, metavar="N",
                     help="First /ofertas page to crawl. Split a big crawl "
                          "across several runs instead of one burst.")

    lg = sub.add_parser(
        "login",
        help="Save a Mercado Libre session (scraping burner or affiliate account)",
    )
    lg.add_argument("--role", choices=["scraping", "affiliate"], default="scraping",
                    help="scraping = burner for search pages (default); "
                         "affiliate = the account that mints your links")
    lg.add_argument("--persist", action="store_true",
                    help="Save a live browser profile instead of a JSON snapshot. "
                         "Only needed if snapshots start getting signed out.")

    sc = sub.add_parser(
        "session-check",
        help="Reopen each saved session and report whether it's still logged in",
    )
    sc.add_argument("--role", choices=["scraping", "affiliate"], default=None,
                    help="Check one role (default: both)")

    nf = sub.add_parser(
        "notify-failure",
        help="Post a Slack alert that a systemd unit failed (used by OnFailure=)",
    )
    nf.add_argument("unit", help="The failed unit name")

    pmode = sub.add_parser(
        "posting",
        help="Show or set the scheduled posting mode (live | simulate | off)",
    )
    pmode.add_argument("mode", nargs="?", choices=list(cfg.POSTING_MODES), default=None,
                       help="live = post to X; simulate = Slack only; off = nothing")

    def _freshness_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-age-hours", type=float, default=None,
                       help="Refuse to run if the data is older than this "
                            "(default: config.max_data_age_hours)")
        p.add_argument("--stale-ok", action="store_true",
                       help="Use the stored data even if it's stale")
        p.add_argument("--llm", action="store_true",
                       help="Generate copy with the LLM instead of the "
                            "deterministic templates")

    sim = sub.add_parser(
        "simulate",
        help="Show exactly what the next `post` would publish, without posting",
    )
    sim.add_argument("--queue", type=int, default=5,
                     help="How many upcoming offers to list after it (default 5)")
    sim.add_argument("--no-links", action="store_true",
                     help="Skip affiliate-link resolution (no browser); render "
                          "the tweet with the plain product URL. Fast format preview.")
    sim.add_argument("--image", action="store_true",
                     help="Also produce the post image the way `post` would "
                          "(screenshot mode captures the ML product page via the "
                          "affiliate session) and report where it saved. Writes "
                          "nothing to the store.")
    _freshness_flags(sim)

    ofr = sub.add_parser(
        "offers",
        help="Browse the offer queue and the tweet each one would produce",
    )
    ofr.add_argument("--limit", type=int, default=10, help="How many offers to show")
    ofr.add_argument("--min-discount", type=int, default=None, metavar="PCT",
                     help="Only show offers at or above this discount, "
                          "overriding the config floor and any per-keyword one")
    _freshness_flags(ofr)

    img = sub.add_parser(
        "images",
        help="Render offer-card images locally and upload them for the posting job",
    )
    img.add_argument("--limit", type=int, default=10,
                     help="How many of the top offers to render (default 10)")
    img.add_argument("--delay", type=float, default=6.0,
                     help="Seconds between captures (default 6)")
    img.add_argument("--redo", action="store_true",
                     help="Re-render even if an image is already stored")
    img.add_argument("--source", choices=["ofertas", "search"], default="ofertas",
                     help="Where to find the card. search covers products ML "
                          "doesn't flag as offers, but needs the scraping session")
    _freshness_flags(img)

    har = sub.add_parser(
        "harvest",
        help="One logged-in sitting: search-scrape, mint links, capture cards",
    )
    har.add_argument("--pages", type=int, default=2,
                     help="Search pages per keyword (default 2)")
    har.add_argument("--images", type=int, default=10,
                     help="How many top offers to capture cards for (default 10)")
    har.add_argument("--no-slack", action="store_true")

    lnk = sub.add_parser(
        "links",
        help="Mint affiliate links for the top offers, in small paced batches",
    )
    lnk.add_argument("--limit", type=int, default=None,
                     help="How many links to mint (default: "
                          "affiliate.max_links_per_ingest)")
    lnk.add_argument("--dry-run", action="store_true",
                     help="List what would be minted without creating anything")
    _freshness_flags(lnk)

    prom = sub.add_parser(
        "promote",
        help="Manually promote one offer — --simulate to Slack, --post to X",
    )
    prom.add_argument("keyword", nargs="?", default=None,
                      help="Fetch a FRESH offer for this keyword via a live "
                           "search scrape — needs `./run login --role scraping` "
                           "(the burner) and hits the login wall risk that "
                           "comes with it. Omit this; use --label instead "
                           "unless you specifically need a live re-scrape.")
    prom.add_argument("--label", default=None, metavar="LABEL",
                      help="Promote the best offer already in the queue whose "
                           "matched_label matches (case-insensitive substring, "
                           "e.g. --label creatina or --label proteina). Filters "
                           "the already-ingested store only — no live scraping, "
                           "no session needed. Use `./run offers` to see labels.")
    mode = prom.add_mutually_exclusive_group()
    mode.add_argument("--simulate", action="store_true",
                      help="Send to Slack (the default)")
    mode.add_argument("--post", action="store_true", help="Publish to X for real")
    prom.add_argument("--dry-run", action="store_true",
                      help="Render only; send nothing")
    prom.add_argument("--pages", type=int, default=2,
                      help="Search pages to scan when a keyword is given (default 2)")
    prom.add_argument("--min-discount", type=int, default=None, metavar="PCT",
                      help="Minimum discount to accept (overrides config)")
    prom.add_argument("--index", type=int, default=1, metavar="N",
                      help="Pick the Nth offer in the matching queue instead of "
                           "the best one (1 = first/default). Matches the "
                           "numbering `./run offers` shows.")
    _freshness_flags(prom)

    db = sub.add_parser("db", help="Run a read-only SQL query against the store")
    db.add_argument("sql", nargs="?", default=None,
                    help="SQL to run (omit to list the built-in named queries)")
    db.add_argument("--name", default=None, help="Run a built-in named query")
    db.add_argument("--csv", action="store_true", help="Output CSV instead of a table")

    po = sub.add_parser(
        "post",
        help="Publish exactly one tweet: the best offer in the queue",
    )
    po.add_argument("--dry-run", action="store_true",
                    help="Print the tweet instead of posting it (same as `simulate`)")
    po.add_argument("--ingest", action="store_true",
                    help="Scrape first, then post from those fresh results")
    po.add_argument("--pages", type=int, default=None, help="Pages to crawl with --ingest")
    _freshness_flags(po)

    rep = sub.add_parser("report", help="Show what's in the store")
    rep.add_argument("--limit", type=int, default=15)
    rep.add_argument("--export", metavar="PATH", default=None,
                     help="Write the full price history to a JSON file")

    setaff = sub.add_parser(
        "set-affiliate",
        help="Configure your affiliate tag from a link generated in the dashboard",
    )
    setaff.add_argument("link", help="Any affiliate link you generated (short links OK)")
    setaff.add_argument("--dry-run", action="store_true",
                        help="Show what was parsed without writing .env")

    sub.add_parser(
        "affiliate-tags",
        help="List the affiliate tags on your account and which one is active",
    )

    chk = sub.add_parser("check-affiliate", help="Print the affiliate link for a URL")
    chk.add_argument("url")

    return p.parse_args()


def _get_store(settings: Any):
    if settings.store == "supabase":
        from supabase_store import SupabaseStore

        return SupabaseStore()
    from store import Store

    return Store(cfg.DB_PATH)


def _norm_categories(categories) -> list[dict]:
    """Normalise a categories config/CLI value into [{"id","label"}, ...]."""
    out = []
    for c in categories or []:
        if isinstance(c, dict) and c.get("id"):
            out.append({"id": c["id"], "label": c.get("label") or c["id"]})
        elif isinstance(c, str) and c.strip():
            out.append({"id": c.strip(), "label": c.strip()})
    return out


def _scrape_and_match(
    settings: Any, *, pages: int, headed: bool, keyword_overrides=None,
    source: str = "ofertas", start_page: int = 1, categories=None,
    capture_cards: bool = False, capture_limit: int = 20,
):
    """Shared by `ingest` and `post --ingest`. Returns (all_products, matched).

    When `categories` is given (and source is the default /ofertas HTTP path),
    each category's offers are crawled in turn and merged, rather than the
    general /ofertas page — a server-side filter for better category coverage.

    With `capture_cards`, the /ofertas source runs through the browser instead
    of HTTP so each keeper's card can be screenshotted from the same page render
    it's scraped from — /ofertas reorders per request, so that's the only
    reliable moment. Up to `capture_limit` cards are captured (page order).
    """
    import auth
    from lib.log import log_ok, log_stage, log_step
    import offers as off
    from scraper import MercadoLibreScraper

    if keyword_overrides:
        keywords = [off.Keyword.parse(k) for k in keyword_overrides]
    else:
        keywords = off.load_keywords(settings)

    where = "/ofertas" if source == "ofertas" else "search"
    span = f"pages {start_page}-{start_page + pages - 1}" if start_page > 1 else f"{pages} pages"
    log_stage(f"Scraping {settings.site} {where} ({span})")
    log_step(f"{len(keywords)} keyword(s): " + ", ".join(k.term for k in keywords))
    log_step(f"scraping session: {auth.describe_session(auth.SCRAPING)}")

    if source == "search" and not auth.has_session(auth.SCRAPING):
        raise SystemExit(
            "Search needs a logged-in Mercado Libre session. Run "
            "`./run login --role scraping` first (use a burner account), or "
            "drop --source search to use /ofertas, which needs no login."
        )

    if source == "search":
        # Search sits behind the login wall plus a JS challenge; only a real
        # browser gets through.
        with MercadoLibreScraper(
            settings.site, headless=not headed,
            delay_sec=settings.delay_between_pages_sec,
        ) as scraper:
            products = scraper.scrape_search([k.term for k in keywords], pages=pages)
    elif headed:
        with MercadoLibreScraper(
            settings.site, headless=False,
            delay_sec=settings.delay_between_pages_sec,
        ) as scraper:
            products = scraper.scrape_offers(pages=pages)
    elif capture_cards:
        # Screenshot each keeper's card inline, from the same page render it was
        # scraped from — /ofertas reorders per request, so a later reload can't
        # re-find it. Needs the browser (with images painted), not the HTTP path.
        products = _scrape_offers_with_capture(
            settings, keywords, pages=pages,
            cats=_norm_categories(categories), capture_limit=capture_limit,
        )
    else:
        # /ofertas is server-rendered, so no browser is needed — ~3x faster and
        # it lets the CI job skip installing Chromium entirely.
        from scraper import scrape_offers_http

        cats = _norm_categories(categories)
        if cats:
            log_step(f"crawling {len(cats)} categor"
                     f"{'y' if len(cats) == 1 else 'ies'}: "
                     + ", ".join(c["label"] for c in cats))
            seen: dict[str, Any] = {}
            for c in cats:
                batch = scrape_offers_http(
                    settings.site, pages=pages, start_page=start_page,
                    delay_sec=settings.delay_between_pages_sec, category=c["id"],
                )
                for pr in batch:
                    seen.setdefault(pr.product_id, pr)
                log_step(f"  {c['label']} ({c['id']}): {len(batch)} products")
            products = list(seen.values())
        else:
            products = scrape_offers_http(
                settings.site, pages=pages, start_page=start_page,
                delay_sec=settings.delay_between_pages_sec,
            )

    log_ok(f"scraped {len(products)} products")
    if len(products) < settings.min_products_expected:
        log_step(
            f"WARNING: expected at least {settings.min_products_expected} products. "
            "MercadoLibre may have changed their markup."
        )

    matched = off.match_products(products, keywords, settings)
    log_ok(f"{len(matched)} matched a keyword")
    for label, n in off.summarize(matched).items():
        log_step(f"  {label}: {n}")
    return products, matched


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _mint_links(store: Any, settings: Any, deals: list[Any], *,
                limit: Optional[int] = None, dry_run: bool = False) -> int:
    """Mint affiliate links in small, spaced batches.

    Kept separate from scraping on purpose. Scraping is anonymous and cheap to
    repeat; minting is authenticated activity on the account the commissions
    belong to, and a long unbroken run of creations is the pattern most likely
    to get that account looked at. Small batches, a pause between them, and a
    hard ceiling per run.
    """
    import random
    import time as _time

    from affiliate_api import AffiliateAPIError, NotAnAffiliateError, create_links
    from lib.log import log_err, log_ok, log_step, log_warn

    aff = settings.affiliate or {}
    tag = settings.affiliate_tag
    if not tag:
        log_err("No affiliate tag configured. Run `./run set-affiliate <link>`.")
        return 0
    if not aff.get("use_link_builder", True):
        log_step("affiliate.use_link_builder is off; nothing to mint")
        return 0

    floor = aff.get("min_discount_for_link", 40)
    cap = limit if limit is not None else aff.get("max_links_per_ingest", 10)
    batch_size = max(1, aff.get("link_batch_size", 5))
    pause = aff.get("delay_between_link_batches_sec", 25)

    wanted = [p for p in deals if (p.discount_pct or 0) >= floor]
    cached = store.get_affiliate_links([p.product_id for p in wanted], tag)
    missing = [p for p in wanted if p.product_id not in cached][:cap]

    log_step(f"{len(wanted)} offer(s) at {floor}%+ · {len(cached)} already have "
             f"links · minting {len(missing)} (cap {cap})")
    if not missing:
        return 0

    if dry_run:
        for p in missing:
            log_step(f"  would mint: {p.discount_pct}% — {p.title[:56]}")
        return 0

    saved = 0
    batches = [missing[i:i + batch_size] for i in range(0, len(missing), batch_size)]
    for n, batch in enumerate(batches, 1):
        log_step(f"batch {n}/{len(batches)} ({len(batch)} link(s))")
        try:
            generated = create_links(
                [p.url for p in batch], site=settings.site, tag=tag,
                allow_browser=aff.get("allow_browser_fallback", True),
            )
        except NotAnAffiliateError as e:
            log_err(str(e))
            return saved
        except AffiliateAPIError as e:
            log_warn(f"link minting stopped ({e})")
            return saved

        by_url = {p.url: p for p in batch}
        for url, pair in generated.items():
            product = by_url.get(url)
            if not product:
                continue
            store.save_affiliate_link(
                product_id=product.product_id, product_url=product.url,
                short_url=pair["short"], full_url=pair["full"], tag=tag,
            )
            saved += 1

        if n < len(batches):
            wait = random.uniform(pause * 0.7, pause * 1.3)
            log_step(f"pausing {wait:.0f}s before the next batch")
            _time.sleep(wait)

    if saved:
        log_ok(f"minted {saved} link(s)")
    return saved


def cmd_ingest(args: argparse.Namespace, settings: Any) -> int:
    from lib.log import log_ok, log_stage, log_step
    import offers as off

    pages = args.pages or settings.pages_per_run
    # --category (repeatable) overrides config.categories; either can be empty.
    categories = args.category if args.category else settings.get("categories")

    # Screenshot each keeper's /ofertas card inline during the scrape (the only
    # reliable moment — /ofertas reorders per request). Only for the anonymous
    # /ofertas source; search products aren't on that page.
    capture_cards = (args.source == "ofertas" and not args.headed
                     and not args.no_capture_cards
                     and settings.get("capture_cards_on_ingest", True))
    capture_limit = (args.capture_limit if args.capture_limit is not None
                     else int(settings.get("max_cards_per_ingest", 20) or 20))

    products, matched = _scrape_and_match(
        settings, pages=pages, headed=args.headed,
        keyword_overrides=args.keyword, source=args.source,
        start_page=args.start_page, categories=categories,
        capture_cards=capture_cards, capture_limit=capture_limit,
    )

    deals = off.filter_offers(matched, settings)
    log_ok(f"{len(deals)} clear the deal thresholds")
    for p in deals[:10]:
        log_step(f"  {p.discount_pct}% off — {p.title[:60]} ({p.matched_label})")

    if args.dry_run:
        log_stage("Dry run — nothing written")
        return 0

    to_store = products if args.all else matched
    log_stage(f"Recording {len(to_store)} price snapshots")
    store = _get_store(settings)
    try:
        run_id = store.start_run("ingest", note=f"{pages} pages")
        n = store.record_snapshots(to_store, run_id)
        store.finish_run(run_id, products_seen=len(products), offers_matched=len(matched))
        log_ok(f"recorded {n} snapshots (run #{run_id})")

        # Cards were screenshotted inline during the scrape (extra['card_path']);
        # compose + upload + cache them now so posting reuses them directly.
        if capture_cards:
            _store_captured_cards(matched, settings, store)

        # Queue depth reflects everything stored, not just this run's haul.
        queue_total = len(_select_deals(store, settings))
    finally:
        store.close()

    if not args.no_slack:
        import notifier

        cats = _norm_categories(categories)
        src = (f"{args.source} · {len(cats)} categories" if cats else args.source)
        notifier.notify(notifier.build_summary(
            site=settings.site, source=src, pages=pages,
            scraped=len(products), matched=len(matched), deals=deals,
            queue_total=queue_total, per_label=off.summarize(matched),
        ))
    return 0


def _require_fresh_data(store: Any, settings: Any, args: argparse.Namespace) -> Optional[int]:
    """Guard against building tweets from stale prices. Returns an exit code to
    bail with, or None to continue."""
    from lib.log import log_err, log_step, log_warn

    max_age = (args.max_age_hours if getattr(args, "max_age_hours", None) is not None
               else settings.max_data_age_hours)
    age = store.data_age_hours()

    if age is None:
        log_err("No scraped data in the store yet.")
        log_step("Run this first:")
        log_step("    ./run ingest")
        return 1

    if age > max_age and not args.stale_ok:
        log_err(f"Scraped data is {age:.1f}h old (limit {max_age:.0f}h) — prices "
                "have probably moved and some offers will have expired.")
        log_step("Refresh it with:")
        log_step("    ./run ingest")
        log_step("...or pass --stale-ok to use it anyway.")
        return 1

    if age > max_age:
        log_warn(f"using stale data ({age:.1f}h old) because --stale-ok was passed")
    else:
        log_step(f"data is {age:.1f}h old — fresh")
    return None


def _diversify(deals: list[Any], recent_labels: list[str], window: int) -> list[Any]:
    """Push offers whose category was used in the last `window` posts to the
    back, so the head of the queue rotates categories instead of letting one
    dominant category post over and over.

    `deals` is already best-discount-first; both partitions preserve that, so
    within "not recently used" the biggest discount still wins — diversity only
    breaks ties across categories, it doesn't override deal quality inside one.
    Falls back to the untouched queue when nothing but recent categories remain.
    """
    if window <= 0 or not deals:
        return deals
    recent = {lbl for lbl in recent_labels[:window] if lbl}
    if not recent:
        return deals
    fresh = [d for d in deals if (d.matched_label or "") not in recent]
    stale = [d for d in deals if (d.matched_label or "") in recent]
    return fresh + stale


def _select_deals(
    store: Any, settings: Any, *, min_discount: Optional[int] = None
) -> list[Any]:
    """The postable queue: keyword-matched, threshold-clearing, off cooldown.

    Ordered best-discount-first and then rotated for category diversity
    (`post_diversity_window`), so the head of this list is always the next
    thing that would go out.
    """
    import offers as off

    stored = store.latest_matched_products()
    matched = off.match_products(stored, off.load_keywords(settings), settings)
    cooldown = store.recently_posted(settings.repost_cooldown_days)
    deals = off.filter_offers(
        matched, settings, exclude_ids=cooldown, min_discount_override=min_discount
    )
    window = int(settings.get("post_diversity_window", 0) or 0)
    if window:
        deals = _diversify(deals, store.recently_posted_labels(window), window)
    deals = _prefer_cached_cards(deals, store)
    return deals


def _prefer_cached_cards(deals: list[Any], store: Any) -> list[Any]:
    """Float offers whose /ofertas card is already cached to the front.

    Cards are captured at ingest time; an offer without one would force the
    unreliable post-time re-find and, on a miss, be withheld from X entirely.
    Preferring a carded offer keeps posting flowing on a real card. Stable, so
    best-discount (and diversity) order is preserved within each group, and it's
    a no-op once ingest captures the whole queue.
    """
    if len(deals) < 2:
        return deals
    have = set(store.get_offer_images([d.product_id for d in deals]))
    if not have or len(have) >= len(deals):
        return deals
    carded = [d for d in deals if d.product_id in have]
    rest = [d for d in deals if d.product_id not in have]
    return carded + rest


def _resolve_links(
    products: list[Any], settings: Any, store: Any, *, allow_untagged: bool
) -> Optional[dict[str, str]]:
    """Map product_id -> affiliate link for a batch, best source first.

    1. cached links from a previous run
    2. MercadoLibre's own link builder (signed `ref=`, short meli.la URL)
    3. the matt_word/matt_tool param form

    Returns None only when nothing usable could be produced and untagged links
    aren't acceptable.
    """
    from affiliate import AffiliateError, build_link_from_settings
    from lib.log import log_err, log_step, log_warn

    tag = settings.affiliate_tag
    aff_cfg = settings.affiliate or {}
    use_api = aff_cfg.get("use_link_builder", True)

    links: dict[str, str] = {}
    if tag:
        links.update(store.get_affiliate_links([p.product_id for p in products], tag))
        if links:
            log_step(f"{len(links)} link(s) from cache")

    missing = [p for p in products if p.product_id not in links]

    if missing and tag and use_api:
        import auth

        if auth.has_session(auth.AFFILIATE):
            from affiliate_api import (
                AffiliateAPIError, NotAnAffiliateError, create_links,
            )

            try:
                generated = create_links(
                    [p.url for p in missing], site=settings.site, tag=tag,
                    allow_browser=aff_cfg.get("allow_browser_fallback", True),
                )
                by_url = {p.url: p for p in missing}
                for url, pair in generated.items():
                    product = by_url.get(url)
                    if not product:
                        continue
                    link = pair["short"] or pair["full"]
                    links[product.product_id] = link
                    store.save_affiliate_link(
                        product_id=product.product_id, product_url=product.url,
                        short_url=pair["short"], full_url=pair["full"], tag=tag,
                    )
            except NotAnAffiliateError as e:
                log_err(str(e))
                return None
            except AffiliateAPIError as e:
                log_warn(f"link builder unavailable ({e}); falling back to "
                         "matt_word/matt_tool links")
        else:
            log_step("no affiliate session — using matt_word/matt_tool links "
                     "(`./run login --role affiliate` enables real meli.la links)")

    # Whatever's left gets the param form.
    for product in products:
        if product.product_id in links:
            continue
        try:
            links[product.product_id] = build_link_from_settings(product.url, settings)
        except AffiliateError as e:
            if not allow_untagged:
                log_err(str(e))
                return None
            log_warn(f"{e}\n  Using an untagged link.")
            links[product.product_id] = product.url

    return links


def _render(product: Any, link: str, settings: Any, *, use_llm: bool) -> str:
    """Tweet text for one product with an already-resolved link."""
    import tweets as tw

    return tw.build_tweet(product, link, settings, deterministic=not use_llm)


def _make_keep_predicate(keywords: list[Any], settings: Any, limit: int):
    """A `product -> bool` for inline card capture: accept a product if it
    matches a keyword AND clears the deal thresholds, up to `limit` distinct
    products. match_products tags the product (keyword/label) as a side effect,
    which is exactly what the later compose step wants."""
    import offers as off

    picked: set[str] = set()

    def keep(p: Any) -> bool:
        if len(picked) >= limit or p.product_id in picked:
            return False
        matched = off.match_products([p], keywords, settings)
        if not matched or not off.filter_offers(matched, settings):
            return False
        picked.add(p.product_id)
        return True

    return keep


def _scrape_offers_with_capture(
    settings: Any, keywords: list[Any], *, pages: int, cats: list[dict],
    capture_limit: int,
) -> list[Any]:
    """Browser /ofertas scrape that screenshots each keeper's card inline.

    Anonymous (no burner session — a logged-in header would bake the account
    holder's name into the shot), images enabled and captured at SCALE for a
    sharp grab. Each category is crawled in turn and merged.
    """
    from lib.log import log_step
    import screenshot as shot
    from scraper import MercadoLibreScraper

    shots = cfg.STATE_DIR / "shots"
    keep = _make_keep_predicate(keywords, settings, capture_limit)
    targets = [c["id"] for c in cats] if cats else [None]
    if cats:
        log_step("capturing cards while crawling: "
                 + ", ".join(c["label"] for c in cats))

    seen: dict[str, Any] = {}
    with MercadoLibreScraper(
        settings.site, headless=True, use_session=False,
        block_images=False, device_scale_factor=shot.SCALE,
        delay_sec=settings.delay_between_pages_sec,
    ) as scraper:
        for cat in targets:
            batch = scraper.scrape_offers(
                pages, category=cat, capture=keep, shots_dir=shots)
            for pr in batch:
                seen.setdefault(pr.product_id, pr)
    return list(seen.values())


def _store_captured_cards(products: list[Any], settings: Any, store: Any) -> int:
    """Compose, upload and cache every card captured inline during the scrape
    (those carrying extra['card_path']). Returns how many were cached."""
    import card as card_mod
    from lib.log import log_stage, log_step, log_warn

    shots = cfg.STATE_DIR / "shots"
    picks = [p for p in products if p.extra.get("card_path")]
    if not picks:
        return 0

    log_stage(f"Caching {len(picks)} captured offer card(s)")
    n = 0
    for p in picks:
        try:
            composed = card_mod.compose_screenshot(
                p.extra["card_path"], shots / f"{p.product_id}-card.png", product=p)
            try:
                url = store.upload_image(p.product_id, composed)
            except RuntimeError as e:
                log_warn(str(e))
                url = None
            ref = url or str(composed)
            store.save_offer_image(
                product_id=p.product_id, url=ref, local_path=str(composed))
            n += 1
            log_step(f"cached {p.matched_label}: {p.title[:48]}")
        except Exception as e:  # noqa: BLE001 - one card mustn't stop the rest
            log_warn(f"failed to cache card for {p.product_id} "
                     f"({type(e).__name__}: {e})")
    log_step(f"cached {n}/{len(picks)} card(s)")
    return n


def _capture_offer_image(
    product: Any, settings: Any, store: Any, *, sources: Optional[list[str]] = None
) -> Optional[str]:
    """Capture the compact Mercado Libre offer card, cache it, and return its
    stored reference (a URL or local path), or None.

    The mobile-style card — product photo, title, discount pill, price.

    The burner (scraping) session is NEVER used here — the whole reason for the
    category-aware /ofertas capture is to need no session at all. Primary
    attempt is always the anonymous /ofertas (+ configured categories) scan.
    Only if that can't relocate the card does it fall back to a keyword search,
    and that fallback uses the AFFILIATE session, never the burner — by request,
    since burner search pages are what kept tripping the login-wall / visual-
    verification wall this project exists to avoid.

    Be aware: the search fallback still puts *a* login-wall risk on the
    affiliate account (the one your commissions depend on), just not the
    burner's. It's opt-in-by-necessity (last resort only) to keep that
    exposure rare, not eliminate it — the anonymous path is what should carry
    the vast majority of captures.
    """
    import auth
    import card as card_mod
    from lib import mlgate
    from lib.log import log_step
    import screenshot as shot_mod

    shots = cfg.STATE_DIR / "shots"

    # Widened vs. the old 3-page cap: ingest can crawl much deeper (up to
    # pages_per_run, or a full category), so a shallow rescan was missing
    # products that were genuinely on /ofertas, just past page 3.
    anon_max_pages = min(int(settings.get("pages_per_run", 12) or 12), 8)

    attempts: list[dict[str, Any]] = [
        {"source": "ofertas", "term": "", "max_pages": anon_max_pages, "role": None},
    ]

    affiliate_paused = mlgate.status(mlgate.AFFILIATE)["cooldown_remaining_sec"] > 0
    if auth.has_session(auth.AFFILIATE) and product.matched_keyword and not affiliate_paused:
        attempts.append({"source": "search", "term": product.matched_keyword,
                         "max_pages": 2, "role": auth.AFFILIATE})
    elif affiliate_paused:
        log_step("affiliate session paused (walled) — skipping the search fallback")

    raw = None
    for a in attempts:
        raw = shot_mod.capture_offer_card(
            product, shots / f"{product.product_id}.png",
            site=settings.site, source=a["source"], search_term=a["term"],
            max_pages=a["max_pages"], session_role=a["role"],
            # The product may have come from a category-filtered ingest, so a
            # niche category's card can miss the plain /ofertas top pages —
            # try the configured categories too, not just the generic pool.
            categories=settings.get("categories"),
        )
        if raw:
            break
    if not raw:
        return None

    composed = card_mod.compose_screenshot(
        raw, shots / f"{product.product_id}-card.png", product=product
    )
    if store is None:
        return str(composed)

    # On Supabase, push the PNG to Storage so any machine can fetch it; on
    # SQLite the local path is the reference (the droplet reads its own disk).
    try:
        url = store.upload_image(product.product_id, composed)
    except RuntimeError as e:
        log_err(str(e))
        return None
    ref = url or str(composed)
    store.save_offer_image(product_id=product.product_id, url=ref,
                           local_path=str(composed))
    log_step("cached desktop product screenshot")
    return ref


def _render_card_image(product: Any, settings: Any, store: Any = None):
    """Compose an ML-style offer card from data alone; cache it. Returns a ref.

    Unlike _capture_offer_image, this touches no Mercado Libre page: card_html
    fills templates/offer_card.html with the product data we already hold plus
    the product photo, then screenshots it headless. No session, no login wall,
    no rate gate — so it can't time a post out on a walled burner. Returns the
    stored ref (URL or local path), or None so the caller falls back to the
    bare product photo.
    """
    import card_html
    from lib.log import log_err, log_step, log_warn

    out = cfg.STATE_DIR / "shots" / f"{product.product_id}-card.png"
    try:
        card_html.render(product, out, brand=settings.get("tweet_signature", "") or "")
    except Exception as e:  # noqa: BLE001 - any render failure degrades gracefully
        log_warn(f"card render failed ({type(e).__name__}: {e}); using product photo")
        return None

    if store is None:
        log_step("rendered offer card from data")
        return str(out)

    try:
        url = store.upload_image(product.product_id, out)
    except RuntimeError as e:
        log_err(str(e))
        return None
    ref = url or str(out)
    store.save_offer_image(product_id=product.product_id, url=ref, local_path=str(out))
    log_step("rendered and cached offer card from data")
    return ref


def _capture_pdp_image(product: Any, settings: Any, store: Any = None):
    """Screenshot the product's ML detail page (the desktop hero) at post time.

    This is the authentic-looking capture the deal accounts use — ML's real
    price block, cuotas and "medios de pago", not a reconstruction. It uses the
    affiliate session for a single, human-paced load per post (~8/day), gated
    through mlgate's affiliate budget. Returns a cached ref, or None so the
    caller falls back to the bare product photo (never the synthetic card).
    """
    import screenshot as shot_mod
    from lib.log import log_err, log_step

    out = cfg.STATE_DIR / "shots" / f"{product.product_id}-pdp.png"
    shot = shot_mod.capture_product_page(product, out, site=settings.site)
    if not shot:
        return None
    if store is None:
        return str(shot)

    try:
        url = store.upload_image(product.product_id, shot)
    except RuntimeError as e:
        log_err(str(e))
        return None
    ref = url or str(shot)
    store.save_offer_image(product_id=product.product_id, url=ref, local_path=str(shot))
    log_step("cached product-page screenshot")
    return ref


def _resolve_image(product: Any, settings: Any, store: Any = None):
    """Pick the picture for a tweet. Returns (image_url, local_path, ok).

    `ok` is False only when a screenshot-type mode (screenshot/pdp/card) could
    NOT produce its image and has fallen back to the bare product photo. For
    "product"/"none" (and any unknown mode) there is no screenshot to miss, so
    `ok` is True. Callers posting to X use it to refuse a bare-photo fallback.

    "screenshot" the compact ML offer card (poly-card: photo, title, discount
                 pill, price) via _capture_offer_image — anonymous /ofertas
                 (+ configured categories) first, no session at all; only on a
                 miss does it fall back to a keyword search via the AFFILIATE
                 session (never the burner) as a rare last resort. Tight crop,
                 no padding. The default.
    "pdp"        ML's real product page (the desktop hero: gallery + price
                 block + cuotas), via the affiliate session — one gentle load
                 per post. More ML chrome, but padded and session-dependent.
    "card"       composes an ML-style card from data alone (card_html) — never
                 walls, but not pixel-identical to ML, so off by default.
    "product"    the bare product photo.
    Every mode degrades to the bare product photo when its image can't be
    produced — a screenshot that fails never posts the synthetic card.
    """
    from lib.log import log_step

    mode = settings.get("tweet_image_mode", "screenshot")
    if mode == "none":
        return None, None, True

    def _as_pair(ref: str):
        return (ref, None) if ref.startswith("http") else (None, ref)

    # A previously-cached image always wins — no ML request, no re-render.
    if store is not None:
        stored = store.get_offer_images([product.product_id]).get(product.product_id)
        if stored and (stored.startswith("http") or Path(stored).is_file()):
            log_step("using the stored offer image")
            return (*_as_pair(stored), True)

    if mode == "screenshot":
        ref = _capture_offer_image(product, settings, store)
        if ref:
            return (*_as_pair(ref), True)
        log_step("no offer-card screenshot — using the product photo")
    elif mode == "pdp":
        ref = _capture_pdp_image(product, settings, store)
        if ref:
            return (*_as_pair(ref), True)
        log_step("no product-page screenshot — using the product photo")
    elif mode == "card":
        ref = _render_card_image(product, settings, store)
        if ref:
            return (*_as_pair(ref), True)
        log_step("card render unavailable — using the product photo")

    # A screenshot-type mode that reaches here has no image and is degrading to
    # the bare product photo — report ok=False so a real X post can refuse it.
    # "product"/unknown modes have no screenshot to miss, so ok stays True.
    ok = mode not in ("screenshot", "pdp", "card")
    return (product.image or None), None, ok


def _show(product: Any, link: str, text: str, header: str, *, source: bool = False) -> None:
    """Print one rendered tweet.

    `source` adds the plain product URL above the tweet — handy when reviewing,
    since the tweet itself only carries the affiliate link and you can't tell
    what it points at without following it. Kept out of the tweet block so that
    block stays exactly what gets published.
    """
    import tweets as tw

    print()
    print(f"\033[1m─── {header} · {product.discount_pct}% OFF · "
          f"{product.matched_label} · "
          f"{tw.tweet_length(text, link)}/{tw.TWEET_LIMIT} chars\033[0m")
    if source:
        print(f"\033[2mproducto: {product.url}\033[0m")
        if product.image:
            print(f"\033[2mimagen:   {product.image}\033[0m")
    print(text)


def _report_tier(deals: list[Any], settings: Any) -> None:
    """Say whether the pick is in the tier you actually want to be posting.

    The queue is always ordered best-discount-first, so the top item is by
    definition the biggest discount available — this only makes the *quality*
    of that top item legible, so a run that has slipped to picking leftovers is
    obvious instead of looking identical to a good one.
    """
    from lib.log import log_step, log_warn

    preferred = settings.get("preferred_min_discount_pct") or 0
    if not preferred or not deals:
        return

    in_tier = [p for p in deals if (p.discount_pct or 0) >= preferred]
    best = deals[0].discount_pct or 0

    if in_tier:
        log_step(f"{len(in_tier)} offer(s) at {preferred}%+ — posting from that tier")
    else:
        log_warn(
            f"no {preferred}%+ offers left; best available is {best}%. "
            "Run `./run ingest` to refresh — new big discounts jump the queue."
        )


def cmd_simulate(args: argparse.Namespace, settings: Any) -> int:
    """Show exactly what the next `./run post` would publish. Writes nothing."""
    from lib.log import log_ok, log_stage, log_step, log_warn

    store = _get_store(settings)
    try:
        log_stage("Checking the scraped data")
        bail = _require_fresh_data(store, settings, args)
        if bail is not None:
            return bail

        deals = _select_deals(store, settings)
        if not deals:
            log_warn("Nothing cleared the thresholds. Lower min_discount_pct in "
                     "config.json, widen the keywords, or run ./run ingest again.")
            return 0

        log_stage(f"{len(deals)} offer(s) in the queue")
        _report_tier(deals, settings)

        if args.no_links:
            log_step("--no-links: using the plain product URL (no affiliate resolution)")
            link = deals[0].url
        else:
            links = _resolve_links(deals[:1], settings, store, allow_untagged=True)
            if links is None:
                return 1
            link = links[deals[0].product_id]
        text = _render(deals[0], link, settings, use_llm=args.llm)
        _show(deals[0], link, text, "WOULD POST NEXT", source=True)

        if args.image:
            print()
            log_stage("Producing the post image")
            mode = settings.get("tweet_image_mode", "screenshot")
            # store=None keeps simulate read-only: capture/render locally, no upload.
            image_url, image_path, _ = _resolve_image(deals[0], settings, store=None)
            ref = image_url or image_path
            if ref:
                log_ok(f"image ready ({mode}): {ref}")
                if image_path:
                    log_step(f"open it locally:  {image_path}")
            else:
                log_warn("no image produced — a real post would fall back to the "
                         f"product photo ({deals[0].image or 'none'})")

        # The rest of the queue, so it's clear what follows on later runs.
        upcoming = deals[1 : 1 + args.queue]
        if upcoming:
            print()
            log_step(f"then, on the following runs ({len(deals) - 1} more queued):")
            for i, p in enumerate(upcoming, 2):
                log_step(f"  {i}. {p.discount_pct}% off — {p.title[:56]} "
                         f"({p.matched_label})")

        print()
        log_ok("simulation only — nothing posted, nothing written to the store")
        log_step("Publish this one with:  ./run post")
        return 0
    finally:
        store.close()


def cmd_offers(args: argparse.Namespace, settings: Any) -> int:
    """Browse the offer queue and the tweet each one would produce."""
    from lib.log import log_ok, log_stage, log_step, log_warn

    store = _get_store(settings)
    try:
        log_stage("Checking the scraped data")
        bail = _require_fresh_data(store, settings, args)
        if bail is not None:
            return bail

        deals = _select_deals(store, settings, min_discount=args.min_discount)
        gate = (f" at {args.min_discount}%+ off" if args.min_discount is not None else "")
        log_stage(f"{len(deals)} offer(s) in the queue{gate}")
        if not deals:
            if args.min_discount is not None:
                log_warn(f"Nothing at {args.min_discount}%+ off. Try a lower "
                         "--min-discount, or ./run ingest for fresher data.")
            else:
                log_warn("Nothing cleared the thresholds. Lower min_discount_pct "
                         "in config.json, widen the keywords, or re-run ingest.")
            return 0

        shown = deals[: args.limit]
        links = _resolve_links(shown, settings, store, allow_untagged=True)
        if links is None:
            return 1
        for i, product in enumerate(shown, 1):
            link = links[product.product_id]
            text = _render(product, link, settings, use_llm=args.llm)
            _show(product, link, text, f"{i}/{len(shown)}", source=True)

        print()
        log_ok(f"{len(shown)} tweet(s) rendered (preview only)")
        log_step("Post the top one with:  ./run post")
        return 0
    finally:
        store.close()


# Handy queries, so you don't have to remember the schema.
NAMED_QUERIES: dict[str, tuple[str, str]] = {
    "offers": (
        "Best current discounts",
        """SELECT h.discount_pct AS pct, p.matched_label AS cat,
                  substr(p.title,1,48) AS title, h.original_price AS was, h.price AS now
           FROM products p JOIN price_history h ON h.id = (
                SELECT id FROM price_history WHERE product_id = p.product_id
                ORDER BY observed_at DESC, id DESC LIMIT 1)
           ORDER BY pct DESC LIMIT 25""",
    ),
    "movers": (
        "Products whose price changed between snapshots",
        """SELECT substr(p.title,1,44) AS title, COUNT(*) AS snaps,
                  MIN(h.price) AS min_price, MAX(h.price) AS max_price,
                  ROUND((MAX(h.price)-MIN(h.price))*100.0/MAX(h.price),1) AS spread_pct
           FROM price_history h JOIN products p USING (product_id)
           GROUP BY h.product_id HAVING COUNT(DISTINCT h.price) > 1
           ORDER BY spread_pct DESC LIMIT 25""",
    ),
    "history": (
        "Full price history, newest first",
        """SELECT h.observed_at AS at, substr(p.title,1,44) AS title,
                  h.price, h.original_price AS was, h.discount_pct AS pct
           FROM price_history h JOIN products p USING (product_id)
           ORDER BY h.observed_at DESC LIMIT 50""",
    ),
    "categories": (
        "How many products matched each keyword",
        """SELECT matched_label AS category, COUNT(*) AS products
           FROM products WHERE matched_label != ''
           GROUP BY matched_label ORDER BY products DESC""",
    ),
    "posted": (
        "Tweets sent, newest first",
        """SELECT posted_at AS at, target, matched_label AS cat,
                  discount_pct AS pct, substr(title,1,40) AS title,
                  tweet_url, affiliate_url
           FROM posts WHERE dry_run = 0 ORDER BY posted_at DESC LIMIT 25""",
    ),
    "runs": (
        "Ingest runs",
        """SELECT id, started_at AS at, kind, products_seen, offers_matched, note
           FROM runs ORDER BY started_at DESC LIMIT 25""",
    ),
    "stale": (
        "How old the data is",
        """SELECT MAX(observed_at) AS last_snapshot,
                  ROUND((julianday('now') - julianday(MAX(observed_at)))*24, 1) AS hours_ago,
                  COUNT(*) AS total_snapshots
           FROM price_history""",
    ),
}


def cmd_images(args: argparse.Namespace, settings: Any) -> int:
    """Capture Mercado Libre's own offer card for the top offers, locally.

    This is a local-only job on purpose. It drives a browser against /ofertas,
    which is exactly the traffic that shouldn't come from a datacenter IP on a
    schedule. The rendered image is uploaded so the GitHub posting job — which
    can't see this machine — can attach it.
    """
    import random
    import time as _time

    from lib.log import log_ok, log_stage, log_step, log_warn

    store = _get_store(settings)
    try:
        log_stage("Checking the scraped data")
        bail = _require_fresh_data(store, settings, args)
        if bail is not None:
            return bail

        deals = _select_deals(store, settings)[: args.limit]
        if not deals:
            log_warn("No offers in the queue. Run `./run ingest` first.")
            return 0

        have = {} if args.redo else store.get_offer_images([p.product_id for p in deals])
        todo = [p for p in deals if p.product_id not in have]
        log_stage(f"{len(todo)} image(s) to render ({len(have)} already stored)")
        if not todo:
            return 0

        source = getattr(args, "source", "ofertas")
        made = 0
        for i, product in enumerate(todo, 1):
            log_step(f"[{i}/{len(todo)}] {product.discount_pct}% — {product.title[:46]}")
            if _capture_offer_image(product, settings, store, sources=[source]):
                made += 1
            # The rate gate already paces the page loads; this is a small extra
            # breath between products so a batch never looks metronomic.
            if i < len(todo):
                _time.sleep(random.uniform(args.delay * 0.6, args.delay * 1.4))

        log_ok(f"{made} image(s) ready for posting")
        return 0
    finally:
        store.close()


def cmd_harvest(args: argparse.Namespace, settings: Any) -> int:
    """Everything that needs Mercado Libre, in one logged-in sitting.

    Sessions don't survive long — both of ours died within a day, and a big
    search crawl seems to be part of what kills them. So rather than logging in
    for each operation, do one login and take everything in that window:
    products, affiliate links and card images. The posting job then runs off
    the database for days without touching Mercado Libre at all.
    """
    import argparse as _argparse

    import auth
    from lib.log import log_err, log_ok, log_stage, log_step

    if not auth.has_session(auth.SCRAPING):
        log_err("Harvest needs the scraping session. Run "
                "`./run login --role scraping` (burner account) first.")
        return 1

    log_stage("Harvest — one session, three jobs")
    log_step(f"scraping: {auth.describe_session(auth.SCRAPING)}")
    log_step(f"affiliate: {auth.describe_session(auth.AFFILIATE)}")

    ingest_args = _argparse.Namespace(
        pages=args.pages, dry_run=False, headed=False, keyword=None, all=False,
        source="search", no_slack=args.no_slack, start_page=1,
    )
    rc = cmd_ingest(ingest_args, settings)
    if rc != 0:
        return rc

    if args.images:
        log_stage("Capturing offer cards from the search listings")
        image_args = _argparse.Namespace(
            limit=args.images, delay=6.0, redo=False, source="search",
            max_age_hours=None, stale_ok=True, llm=False,
        )
        cmd_images(image_args, settings)

    log_ok("harvest complete")
    log_step("Affiliate links are NOT minted here — that's account activity, "
             "and it shouldn't ride along with a scrape.")
    log_step("Mint them separately, when you're ready:  ./run links")
    return 0


def cmd_links(args: argparse.Namespace, settings: Any) -> int:
    """Mint affiliate links — a deliberate, separate step from scraping."""
    from lib.log import log_stage, log_step

    store = _get_store(settings)
    try:
        log_stage("Checking the scraped data")
        bail = _require_fresh_data(store, settings, args)
        if bail is not None:
            return bail

        deals = _select_deals(store, settings)
        log_stage(f"{len(deals)} offer(s) in the queue")
        _mint_links(store, settings, deals, limit=args.limit, dry_run=args.dry_run)
        if args.dry_run:
            log_step("dry run — nothing was created in your affiliate account")
        return 0
    finally:
        store.close()


def cmd_db(args: argparse.Namespace, settings: Any) -> int:
    """Run SQL against the SQLite store and print the result as a table."""
    import csv
    import sqlite3

    from lib.log import log_err, log_step

    if settings.store != "sqlite":
        log_err(f'`db` only works with the SQLite store (store is "{settings.store}"). '
                "Query Supabase from its own SQL editor.")
        return 1

    sql = args.sql
    if args.name:
        if args.name not in NAMED_QUERIES:
            log_err(f"Unknown query '{args.name}'.")
            sql = None
        else:
            sql = NAMED_QUERIES[args.name][1]

    if not sql:
        log_step("Built-in queries — run one with:  ./run db --name <name>")
        for name, (desc, _) in NAMED_QUERIES.items():
            print(f"    {name:12} {desc}")
        log_step("Or pass any SQL:  ./run db \"SELECT * FROM products LIMIT 5\"")
        log_step("Tables: products, price_history, posts, runs")
        return 0

    if not cfg.DB_PATH.is_file():
        log_err(f"No database at {cfg.DB_PATH}. Run `./run ingest` first.")
        return 1

    # Read-only connection: a typo in a query can't damage the history.
    conn = sqlite3.connect(f"file:{cfg.DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in (cur.description or [])]
    except sqlite3.Error as e:
        log_err(f"SQL error: {e}")
        return 1
    finally:
        conn.close()

    if not rows:
        log_step("(no rows)")
        return 0

    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        w.writerows(rows)
        return 0

    def cell(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:,.0f}".replace(",", ".") if v >= 1000 else f"{v:g}"
        return "" if v is None else str(v)

    table = [cols] + [[cell(v) for v in r] for r in rows]
    widths = [min(max(len(r[i]) for r in table), 50) for i in range(len(cols))]
    sep = "─┼─".join("─" * w for w in widths)
    for i, r in enumerate(table):
        print(" │ ".join(c[:w].ljust(w) for c, w in zip(r, widths)))
        if i == 0:
            print(sep)
    print(f"\n{len(rows)} row(s)")
    return 0


def _alert_missing_screenshot(product: Any, settings: Any, mode: str) -> None:
    """Slack the fact that a post was withheld because its screenshot failed.

    The offer isn't recorded as posted, so it stays in the queue and the next
    run retries — screenshot misses are usually transient (a walled search
    fallback, a card that briefly dropped off /ofertas)."""
    import notifier

    disc = f"{product.discount_pct}% OFF · " if product.discount_pct else ""
    notifier.notify(
        f"⚠️ *ml-referrals — post skipped (no {mode} image)*\n"
        f"Did not post to X: the offer-card screenshot couldn't be generated, "
        f"and posting a bare product photo is disabled.\n"
        f"• {disc}{product.title[:90]}\n"
        f"   {product.url}\n"
        f"_Left queued — the next run retries. If it keeps failing, check the "
        f"affiliate session / `./run simulate --image`._"
    )


def _publish_one(
    product: Any, settings: Any, store: Any, *,
    target: str, dry_run: bool = False, use_llm: bool = False,
) -> int:
    """Publish one product to `target` (twitter_api / twitter_cookie / slack).

    The shared path behind `post` and `promote`: resolve the affiliate link,
    render the tweet, capture the image, send, and record. Returns 0 on success
    or a clean skip, 1 on error.
    """
    import tweets as tw
    from lib.log import log_err, log_ok, log_step, log_warn
    from lib.twitter_post import TwitterPoster, TwitterPostError

    # Targeting the API without keys: skip cleanly *before* spending any
    # Mercado Libre requests, so scheduled runs don't alert during setup.
    if target in ("twitter_api", "twitter") and not dry_run:
        from lib.twitter_api import have_credentials
        if not have_credentials():
            log_warn("post_target is 'twitter_api' but the API keys aren't set "
                     "(TWITTER_API_KEY/SECRET, TWITTER_ACCESS_TOKEN/SECRET).\n"
                     "  Skipping — add them to .env to go live.")
            return 0

    links = _resolve_links([product], settings, store, allow_untagged=dry_run)
    if links is None:
        return 1
    link = links[product.product_id]
    text = _render(product, link, settings, use_llm=use_llm)
    _show(product, link, text, "DRY RUN" if dry_run else f"POSTING → {target.upper()}")

    print()
    image_url, image_path, image_ok = _resolve_image(product, settings, store)

    # A screenshot-type image mode that couldn't produce its image degrades to
    # the bare product photo. That's fine for the Slack simulator (you want to
    # SEE that it fell back), but a real X post must NOT go out photo-only —
    # skip it and alert on Slack instead, so the offer stays queued for a retry.
    if (not image_ok and not dry_run
            and target in ("twitter_api", "twitter", "twitter_cookie")):
        mode = settings.get("tweet_image_mode", "screenshot")
        log_err(f"No {mode} image was generated — not posting to X "
                "(a bare product photo is not allowed). Alerting on Slack.")
        _alert_missing_screenshot(product, settings, mode)
        return 0

    if target == "slack":
        tweet_id = None
        if not dry_run:
            import notifier
            # Slack renders only a public URL; a local path can't be shown, so
            # fall back to the product photo for the preview.
            preview = image_url or (product.image if image_path else None)
            try:
                sent = notifier.post_tweet_to_slack(text, image_url=preview)
            except Exception as e:  # noqa: BLE001
                log_err(f"slack post failed: {e}")
                return 1
            if not sent:
                log_err("target is 'slack' but SLACK_WEBHOOK_URL isn't set.")
                return 1
            tweet_id = "slack"

    elif target in ("twitter_api", "twitter"):
        from lib.twitter_api import (
            MissingCredentials, TwitterAPIError, TwitterAPIPoster,
        )
        try:
            tweet_id = TwitterAPIPoster(dry_run=dry_run).post(
                text, image_url=image_url, image_path=image_path)
        except MissingCredentials as e:
            log_warn(f"{e}\n  Skipping until the API keys are set.")
            return 0
        except TwitterAPIError as e:
            log_err(f"post failed: {e}")
            return 1

    elif target == "twitter_cookie":
        try:
            tweet_id = TwitterPoster(cache_dir=cfg.STATE_DIR, dry_run=dry_run).post(
                text, image_url=image_url, image_path=image_path)
        except TwitterPostError as e:
            log_err(f"post failed: {e}")
            return 1
    else:
        log_err(f"unknown target '{target}' — use twitter_api, twitter_cookie, or slack")
        return 1

    # A real X post has a numeric status id and a canonical permalink; Slack
    # posts ("slack") and dry runs (None) have neither.
    tweet_url = (
        f"https://x.com/i/status/{tweet_id}"
        if tweet_id and tweet_id.isdigit() else None
    )
    store.record_post(
        product=product, tweet_id=tweet_id, tweet_url=tweet_url, tweet_text=text,
        affiliate_url=link, target=target,
        char_count=tw.tweet_length(text, link),
        has_image=bool(image_url or image_path), dry_run=dry_run,
    )
    if dry_run:
        log_ok("dry run — nothing published")
        return 0
    if target == "slack":
        log_ok("posted to Slack (simulator)")
        return 0
    if tweet_id:
        url = f"https://x.com/i/status/{tweet_id}"
        log_ok(f"posted {url}")
        # Mirror every real X post to Slack, so the channel is a running log of
        # what actually went out (not just the simulator).
        if settings.get("mirror_to_slack", True):
            import notifier
            preview = image_url or (product.image if image_path else None)
            try:
                notifier.post_tweet_to_slack(
                    f":bird: *Posted to X* — {url}\n\n{text}", image_url=preview)
                log_step("mirrored to Slack")
            except Exception as e:  # noqa: BLE001 - a failed mirror mustn't fail the post
                log_warn(f"Slack mirror failed ({type(e).__name__}: {e})")
    return 0


def cmd_promote(args: argparse.Namespace, settings: Any) -> int:
    """Manually promote one offer — optionally for a specific keyword.

    Two modes: --simulate (default) posts to Slack, --post publishes to X. With
    a keyword, it live-scrapes that term (search) so the offer is genuinely
    fresh, then applies the usual thresholds; without one, it takes the best
    fresh offer already in the queue.
    """
    from lib.log import log_err, log_ok, log_stage, log_step, log_warn
    import offers as off

    target = "twitter_api" if args.post else "slack"
    store = _get_store(settings)
    try:
        if args.keyword:
            import auth
            from scraper import MercadoLibreScraper

            if not auth.has_session(auth.SCRAPING):
                log_err("A keyword promote live-scrapes search, which needs "
                        "`./run login --role scraping`.")
                return 1
            log_stage(f"Fetching a fresh offer for '{args.keyword}'")
            with MercadoLibreScraper(
                settings.site, headless=True,
                delay_sec=settings.delay_between_pages_sec,
            ) as scraper:
                products = scraper.scrape_search([args.keyword], pages=args.pages)
            if not products:
                log_warn("Nothing came back for that keyword.")
                return 0
            # Tag against the keyword and snapshot, so it lands in the store /
            # price history like any other offer.
            kw = [off.Keyword(term=args.keyword, label=args.keyword.title())]
            matched = off.match_products(products, kw, settings)
            run_id = store.start_run("promote", note=args.keyword)
            store.record_snapshots(matched or products, run_id)
            cooldown = store.recently_posted(settings.repost_cooldown_days)
            deals = off.filter_offers(
                matched, settings, exclude_ids=cooldown,
                min_discount_override=args.min_discount,
            )
        else:
            log_stage("Checking the scraped data")
            bail = _require_fresh_data(store, settings, args)
            if bail is not None:
                return bail
            deals = _select_deals(store, settings)
            if args.min_discount is not None:
                deals = [p for p in deals if (p.discount_pct or 0) >= args.min_discount]
            if args.label:
                # normalize() (accent-stripping, lowercasing) matches how
                # offers.py itself compares terms — "proteina" must still hit
                # a label stored as "Proteína".
                needle = off.normalize(args.label)
                deals = [p for p in deals if needle in off.normalize(p.matched_label or "")]

        log_stage(f"{len(deals)} matching offer(s)")
        if not deals:
            hint = (f"Nothing in the queue matched --label '{args.label}'. Check "
                    "the label spelling with `./run offers`, or run `./run ingest` "
                    "first if the category hasn't been scraped yet."
                    if args.label else
                    "No fresh offer cleared the thresholds. Try --min-discount "
                    "lower, or a different keyword.")
            log_warn(hint)
            return 0

        idx = args.index - 1
        if idx < 0 or idx >= len(deals):
            log_warn(f"--index {args.index} is out of range — only {len(deals)} "
                     "offer(s) matched. Run `./run offers` to see the numbered list.")
            return 0
        if args.index > 1:
            log_step(f"picking #{args.index} of {len(deals)} (skipping the "
                     f"{args.index - 1} ahead of it)")

        return _publish_one(deals[idx], settings, store, target=target,
                            dry_run=args.dry_run, use_llm=args.llm)
    finally:
        store.close()


def cmd_post(args: argparse.Namespace, settings: Any) -> int:
    """Publish exactly one tweet: the best offer currently in the queue.

    One at a time, always. A burst of affiliate links reads as spam, and a
    single post per run keeps every publish reviewable — run `./run simulate`
    first to see precisely what this will send.
    """
    from lib.log import log_err, log_ok, log_stage, log_step, log_warn
    from lib.twitter_post import TwitterPoster, TwitterPostError

    # The runtime switch (./run posting). "off" disables scheduled posting
    # entirely; a manual --dry-run still previews. This is checked before any
    # Mercado Libre work so a paused pipeline is truly idle.
    mode = cfg.get_posting_mode()
    if mode == "off" and not args.dry_run:
        log_warn("Scheduled posting is OFF — `./run posting live` (or simulate) "
                 "to re-enable.")
        return 0

    store = _get_store(settings)
    try:
        if args.ingest:
            pages = args.pages or settings.pages_per_run
            _, matched = _scrape_and_match(settings, pages=pages, headed=False,
                                           categories=settings.get("categories"))
            run_id = store.start_run("post-ingest")
            store.record_snapshots(matched, run_id)
        else:
            log_stage("Checking the scraped data")
            bail = _require_fresh_data(store, settings, args)
            if bail is not None:
                return bail

        deals = _select_deals(store, settings)
        log_stage(f"{len(deals)} offer(s) in the queue")
        if not deals:
            log_warn("No offer cleared the thresholds. Lower min_discount_pct in "
                     "config.json, or widen the keyword list.")
            return 0
        _report_tier(deals, settings)

        # "simulate" forces Slack; "live" uses the configured target.
        target = "slack" if mode == "simulate" else settings.get("post_target", "twitter_api")
        rc = _publish_one(deals[0], settings, store, target=target,
                          dry_run=args.dry_run, use_llm=args.llm)
        if rc == 0 and not args.dry_run:
            log_step(f"{len(deals) - 1} offer(s) still queued for the next run")
        return rc
    finally:
        store.close()


def cmd_report(args: argparse.Namespace, settings: Any) -> int:
    from lib.log import log_ok, log_stage, log_step

    import auth
    from lib import mlgate

    store = _get_store(settings)
    try:
        log_step(f"scheduled posting: {cfg.get_posting_mode().upper()}")
        log_stage("Mercado Libre sessions")
        for role in auth.ROLES:
            log_step(f"{role:10} {auth.describe_session(role)}")

        log_stage("Rate gate (requests used / budget)")
        for acct in (mlgate.ANON, mlgate.SCRAPING, mlgate.AFFILIATE):
            st = mlgate.status(acct)
            cd = st["cooldown_remaining_sec"]
            note = f"  COOLDOWN {cd // 60}m" if cd else ""
            log_step(f"{acct:10} {st['in_last_hour']}/h {st['in_last_day']}/day{note}")

        stats = store.stats()
        log_stage("Store")
        for k, v in stats.items():
            log_step(f"{k:14} {v}")

        log_stage(f"Top {args.limit} offers by discount")
        for row in store.top_offers(args.limit):
            log_step(
                f"{str(row.get('discount_pct') or '?'):>4}%  "
                f"{(row.get('title') or '')[:58]:58}  "
                f"{row.get('price')}"
            )

        if args.export:
            n = store.export_json(args.export)
            log_ok(f"exported {n} price rows to {args.export}")
    finally:
        store.close()
    return 0


def _write_env(updates: dict[str, str]) -> None:
    """Set keys in .env, preserving everything else in the file."""
    path = cfg.ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []

    for key, value in updates.items():
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}=") or line.strip().startswith(f"#{key}="):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def cmd_set_affiliate(args: argparse.Namespace, settings: Any) -> int:
    """Configure the affiliate identity from a link generated in the dashboard.

    Affiliate attribution is just two query params that never change per
    product, so one dashboard link is enough to tag every future link.
    """
    from affiliate import build_link, parse_affiliate_link
    from lib.log import log_err, log_ok, log_step, log_warn

    info = parse_affiliate_link(args.link)
    if info["resolved"] != args.link:
        log_step(f"resolved to: {info['resolved'][:110]}")

    if not info["tag"] or not info["tool"]:
        log_err("Couldn't find matt_word and matt_tool in that link.")
        log_step("Generate a link from a product page while logged in as an "
                 "affiliate, then paste the whole thing (short links are fine).")
        log_step("Expected something containing: ?matt_word=<tag>&matt_tool=<id>")
        return 1

    log_ok(f"tag  (matt_word): {info['tag']}")
    log_ok(f"tool (matt_tool): {info['tool']}")

    if info["shape"] == "social":
        log_warn(
            "This is a /social/ wrapper link. The default param form probably "
            "still tracks, but if your dashboard reports nothing, set "
            "affiliate.link_template in config.json to match the wrapper shape."
        )

    if args.dry_run:
        log_step("--dry-run: .env not modified")
        return 0

    _write_env({
        "ML_AFFILIATE_TAG": info["tag"],
        "ML_AFFILIATE_TOOL_ID": info["tool"],
    })
    log_ok(f"written to {cfg.ENV_FILE} (gitignored)")

    sample = build_link(
        "https://www.mercadolibre.com.ar/producto-ejemplo/p/MLA12345678",
        tag=info["tag"], tool_id=info["tool"],
        extra_params=(settings.affiliate or {}).get("extra_params"),
    )
    print()
    log_step("every link will now look like:")
    log_step(f"  {sample}")
    print()
    log_step("Check it against your dashboard link, then:  ./run simulate")
    return 0


def cmd_affiliate_tags(args: argparse.Namespace, settings: Any) -> int:
    """Show the tags Mercado Libre has on this account, and flag mismatches."""
    from affiliate_api import AffiliateAPIError, AffiliateLinkBuilder
    from lib.log import log_err, log_ok, log_step, log_warn

    configured = settings.affiliate_tag
    try:
        with AffiliateLinkBuilder(settings.site, configured or "x") as builder:
            tags = builder.list_tags()
    except AffiliateAPIError as e:
        log_err(str(e))
        return 1

    if not tags:
        log_warn("No tags found on the link builder page — Mercado Libre may "
                 "have changed it. Check the dashboard directly.")
        return 1

    log_step(f"{len(tags)} tag(s) on this account:")
    for t in tags:
        mark = "\033[32m● in use\033[0m" if t.get("in_use") else "  unused "
        here = "  \033[1m<- configured\033[0m" if t["tag"] == configured else ""
        log_step(f"  {mark}  {t['tag']:22} {t.get('generated_date', '')[:19]}{here}")

    names = [t["tag"] for t in tags]
    if not configured:
        log_warn("No tag configured. Set one with `./run set-affiliate <link>`.")
        return 1
    if configured not in names:
        log_err(f"Configured tag '{configured}' isn't on this account. "
                "Commissions won't be credited — fix ML_AFFILIATE_TAG in .env.")
        return 1

    active = next((t["tag"] for t in tags if t.get("in_use")), None)
    if active and active != configured:
        log_warn(f"'{configured}' is configured but '{active}' is the one marked "
                 "in use on Mercado Libre.")
        return 0

    log_ok(f"links are being generated with '{configured}'")
    log_step("Note: the /social/<slug> part of a link is your profile slug, not "
             "the tag — the tag is the matt_word= param.")
    return 0


def cmd_check_affiliate(args: argparse.Namespace, settings: Any) -> int:
    from affiliate import AffiliateError, build_link_from_settings
    from lib.log import log_ok, log_step

    try:
        link = build_link_from_settings(args.url, settings)
    except AffiliateError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    log_step(f"input:     {args.url}")
    log_ok(f"affiliate: {link}")
    log_step("Compare this against a link generated in your affiliate dashboard. "
             "If the shape differs, set affiliate.link_template in config.json.")
    return 0


def cmd_login(args: argparse.Namespace, settings: Any) -> int:
    import auth

    return auth.login(settings.site, args.role, persist=args.persist)


_MODE_MEANS = {
    "live": "the scheduled post publishes to X"
            + " (and mirrors to Slack)",
    "simulate": "the scheduled post publishes to Slack only — a dry run on a timer",
    "off": "the scheduled post does nothing (ingest and session-check keep running)",
}


def cmd_posting(args: argparse.Namespace, settings: Any) -> int:
    """Show or set the scheduled posting mode — the runtime on/off switch."""
    from lib.log import log_ok, log_stage, log_step

    if not args.mode:
        current = cfg.get_posting_mode()
        log_stage(f"Scheduled posting mode: {current.upper()}")
        log_step(_MODE_MEANS[current])
        print()
        log_step("change it with:  ./run posting <live|simulate|off>")
        for m in cfg.POSTING_MODES:
            mark = "→" if m == current else " "
            log_step(f"  {mark} {m:9} {_MODE_MEANS[m]}")
        return 0

    cfg.set_posting_mode(args.mode)
    log_ok(f"scheduled posting is now: {args.mode.upper()}")
    log_step(_MODE_MEANS[args.mode])
    log_step("Manual `./run promote --post` / `--simulate` still work regardless.")
    return 0


def cmd_notify_failure(args: argparse.Namespace, settings: Any) -> int:
    """Announce a failed systemd unit to Slack.

    Wired as OnFailure= on each service, so *any* non-zero exit — a crash, an
    unhandled exception, or a handled error that returns 1 — reaches you,
    instead of dying quietly in the journal on the droplet.
    """
    from datetime import datetime
    import notifier

    unit = args.unit
    notifier.notify(
        f":rotating_light: *ml-referrals* — `{unit}` failed at "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M %Z')}.\n"
        f"Logs:  `journalctl -u {unit} -n 40 --no-pager`"
    )
    return 0


def cmd_session_check(args: argparse.Namespace, settings: Any) -> int:
    """Reopen saved sessions and confirm they're still authenticated.

    The round-trip longevity test: run it on a timer and it tells you the moment
    a session dies (and, by how long it lasted, whether persistent profiles are
    holding). Posts to Slack when a session is found dead.
    """
    import auth
    from lib.log import log_err, log_ok, log_stage, log_step

    roles = [args.role] if args.role else list(auth.ROLES)
    any_dead = False
    for role in roles:
        log_stage(f"Checking the {role} session")
        res = auth.session_check(settings.site, role)
        line = f"{role}: {res['account']} — {res['detail']}"
        if res["alive"]:
            log_ok(line)
        else:
            log_err(line)
            any_dead = True
            try:
                import notifier
                notifier.notify(f":warning: *ml-referrals* — the *{role}* Mercado "
                                f"Libre session is dead ({res['detail']}). "
                                f"Re-run `./run login --role {role}` and rsync the "
                                f"profile to the droplet.")
            except Exception:  # noqa: BLE001
                pass
    return 1 if any_dead else 0


def main() -> int:
    args = _parse_args()
    cfg.bootstrap()
    settings = cfg.load_settings()

    handlers = {
        "login": cmd_login,
        "session-check": cmd_session_check,
        "notify-failure": cmd_notify_failure,
        "posting": cmd_posting,
        "ingest": cmd_ingest,
        "simulate": cmd_simulate,
        "offers": cmd_offers,
        "images": cmd_images,
        "harvest": cmd_harvest,
        "links": cmd_links,
        "db": cmd_db,
        "post": cmd_post,
        "promote": cmd_promote,
        "report": cmd_report,
        "set-affiliate": cmd_set_affiliate,
        "affiliate-tags": cmd_affiliate_tags,
        "check-affiliate": cmd_check_affiliate,
    }
    try:
        return handlers[args.command](args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
