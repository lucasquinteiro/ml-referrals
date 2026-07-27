#!/usr/bin/env python3
"""
ml-referrals — main entrypoint.

Pipeline, in three commands:

    ./run ingest            scrape MercadoLibre /ofertas, match against the
                            keywords in config.json, snapshot every price
    ./run post              pick the best un-posted offers, build affiliate
                            links, tweet them
    ./run report            what's in the store right now

Typical local test drive:

    ./run ingest --dry-run          # scrape + match, write nothing
    ./run ingest                    # scrape + record price snapshots
    ./run post --dry-run            # show the tweets that would go out
    ./run post --limit 1            # actually tweet one

    ./run check-affiliate <url>     # sanity-check your affiliate link shape
    ./run report --export prices.json

In GitHub Actions, `ingest` runs on a schedule and `post` a couple of times a
day — see .github/workflows/.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

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

    sub.add_parser("login", help="Log in to Mercado Libre once and save the session")

    po = sub.add_parser("post", help="Tweet the best offers found so far")
    po.add_argument("--limit", type=int, default=None,
                    help="How many tweets to send (default: config.tweets_per_run)")
    po.add_argument("--dry-run", action="store_true",
                    help="Print the tweets instead of posting them")
    po.add_argument("--ingest", action="store_true",
                    help="Scrape first, then post from those fresh results")
    po.add_argument("--pages", type=int, default=None, help="Pages to crawl with --ingest")

    rep = sub.add_parser("report", help="Show what's in the store")
    rep.add_argument("--limit", type=int, default=15)
    rep.add_argument("--export", metavar="PATH", default=None,
                     help="Write the full price history to a JSON file")

    chk = sub.add_parser("check-affiliate", help="Print the affiliate link for a URL")
    chk.add_argument("url")

    return p.parse_args()


def _get_store(settings: Any):
    if settings.store == "supabase":
        from supabase_store import SupabaseStore

        return SupabaseStore()
    from store import Store

    return Store(cfg.DB_PATH)


def _scrape_and_match(
    settings: Any, *, pages: int, headed: bool, keyword_overrides=None, source: str = "ofertas"
):
    """Shared by `ingest` and `post --ingest`. Returns (all_products, matched)."""
    import auth
    from lib.log import log_ok, log_stage, log_step
    import offers as off
    from scraper import MercadoLibreScraper

    if keyword_overrides:
        keywords = [off.Keyword.parse(k) for k in keyword_overrides]
    else:
        keywords = off.load_keywords(settings)

    where = "/ofertas" if source == "ofertas" else "search"
    log_stage(f"Scraping {settings.site} {where} ({pages} pages)")
    log_step(f"{len(keywords)} keyword(s): " + ", ".join(k.term for k in keywords))
    log_step(f"session: {auth.describe_session()}")

    if source == "search" and not auth.has_session():
        raise SystemExit(
            "Search needs a logged-in Mercado Libre session. Run `./run login` "
            "first, or drop --source search to use /ofertas (no login needed)."
        )

    with MercadoLibreScraper(
        settings.site, headless=not headed, delay_sec=settings.delay_between_pages_sec
    ) as scraper:
        if source == "search":
            products = scraper.scrape_search([k.term for k in keywords], pages=pages)
        else:
            products = scraper.scrape_offers(pages=pages)

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


def cmd_ingest(args: argparse.Namespace, settings: Any) -> int:
    from lib.log import log_ok, log_stage, log_step
    import offers as off

    pages = args.pages or settings.pages_per_run
    products, matched = _scrape_and_match(
        settings, pages=pages, headed=args.headed,
        keyword_overrides=args.keyword, source=args.source,
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
    finally:
        store.close()
    return 0


def cmd_post(args: argparse.Namespace, settings: Any) -> int:
    from affiliate import AffiliateError, build_link_from_settings
    from lib.log import log_err, log_ok, log_stage, log_step, log_warn
    from lib.twitter_post import TwitterPoster, TwitterPostError
    import offers as off
    import tweets as tw

    limit = args.limit if args.limit is not None else settings.tweets_per_run
    store = _get_store(settings)

    try:
        # Source of candidates: a fresh scrape, or the last one we stored.
        if args.ingest:
            pages = args.pages or settings.pages_per_run
            _, matched = _scrape_and_match(settings, pages=pages, headed=False)
            if not args.dry_run:
                run_id = store.start_run("post-ingest")
                store.record_snapshots(matched, run_id)
        else:
            stored = store.latest_matched_products()
            if not stored:
                log_err("Nothing recent in the store. Run `./run ingest` first, "
                        "or use `./run post --ingest`.")
                return 1
            # Re-match against the *current* config so edits to keywords and
            # their per-keyword thresholds take effect without re-scraping.
            matched = off.match_products(stored, off.load_keywords(settings), settings)
            log_step(f"{len(matched)} stored offer(s) still match the config")

        cooldown = store.recently_posted(settings.repost_cooldown_days)
        deals = off.filter_offers(matched, settings, exclude_ids=cooldown)
        log_stage(f"{len(deals)} postable offer(s); sending up to {limit}")
        if not deals:
            log_warn("No offer cleared the thresholds. Lower min_discount_pct in "
                     "config.json, or widen the keyword list.")
            return 0

        poster = TwitterPoster(cache_dir=cfg.STATE_DIR, dry_run=args.dry_run)

        sent = 0
        for product in deals:
            if sent >= limit:
                break
            try:
                link = build_link_from_settings(product.url, settings)
            except AffiliateError as e:
                # A real post without a tag is lost commission, so that stays
                # fatal — but a dry run should still let you review the copy.
                if not args.dry_run:
                    log_err(str(e))
                    return 1
                if sent == 0:
                    log_warn(f"{e}\n  Previewing with an untagged link.")
                link = product.url

            text = tw.build_tweet(product, link, settings)
            log_step(f"[{sent + 1}/{limit}] {product.discount_pct}% off — "
                     f"{product.title[:50]} "
                     f"({tw.tweet_length(text, link)}/{tw.TWEET_LIMIT} chars)")

            try:
                tweet_id = poster.post(text)
            except TwitterPostError as e:
                log_err(f"post failed: {e}")
                return 1

            store.record_post(
                product_id=product.product_id,
                tweet_id=tweet_id,
                tweet_text=text,
                affiliate_url=link,
                price=product.price,
                discount_pct=product.discount_pct,
                dry_run=args.dry_run,
            )
            if tweet_id:
                log_ok(f"posted https://x.com/i/status/{tweet_id}")
            sent += 1

            if sent < limit and not args.dry_run:
                time.sleep(settings.delay_between_tweets_sec)

        log_ok(f"{sent} tweet(s) {'previewed' if args.dry_run else 'posted'}")
    finally:
        store.close()
    return 0


def cmd_report(args: argparse.Namespace, settings: Any) -> int:
    from lib.log import log_ok, log_stage, log_step

    store = _get_store(settings)
    try:
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

    return auth.login(settings.site)


def main() -> int:
    args = _parse_args()
    cfg.bootstrap()
    settings = cfg.load_settings()

    handlers = {
        "login": cmd_login,
        "ingest": cmd_ingest,
        "post": cmd_post,
        "report": cmd_report,
        "check-affiliate": cmd_check_affiliate,
    }
    try:
        return handlers[args.command](args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
