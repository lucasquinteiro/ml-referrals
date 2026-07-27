#!/usr/bin/env python3
"""
ml-referrals — main entrypoint.

Two steps: scrape, then turn what was scraped into tweets.

    ./run ingest            scrape MercadoLibre, match against the keywords in
                            config.json, snapshot every price
    ./run offers            find the offers in that data and render the tweet
                            for each one (preview by default, --post to send)

    ./run db                query the store
    ./run report            summary of what's in the store
    ./run login             save a session, unlocking --source search
    ./run post              older direct path: pick + tweet in one go

Typical local test drive:

    ./run ingest                    # scrape + record price snapshots
    ./run offers                    # see the tweets (deterministic copy)
    ./run offers --post --limit 1   # actually publish one

    ./run db --name offers          # best current discounts
    ./run check-affiliate <url>     # sanity-check your affiliate link shape

`offers` refuses to run on data older than config.max_data_age_hours, since a
tweet built from an expired price is worse than no tweet.

In GitHub Actions, `ingest` runs on a schedule and `post` a couple of times a
day — see .github/workflows/.
"""

from __future__ import annotations

import argparse
import sys
import time
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

    sub.add_parser("login", help="Log in to Mercado Libre once and save the session")

    ofr = sub.add_parser(
        "offers",
        help="Find offers in the scraped data and show the tweets for them",
    )
    ofr.add_argument("--limit", type=int, default=10, help="How many offers to show")
    ofr.add_argument("--max-age-hours", type=float, default=None,
                     help="Refuse to run if the data is older than this "
                          "(default: config.max_data_age_hours)")
    ofr.add_argument("--stale-ok", action="store_true",
                     help="Use the stored data even if it's stale")
    ofr.add_argument("--llm", action="store_true",
                     help="Generate copy with the LLM instead of the deterministic templates")
    ofr.add_argument("--post", action="store_true",
                     help="Actually publish the tweets shown (default is preview only)")

    db = sub.add_parser("db", help="Run a read-only SQL query against the store")
    db.add_argument("sql", nargs="?", default=None,
                    help="SQL to run (omit to list the built-in named queries)")
    db.add_argument("--name", default=None, help="Run a built-in named query")
    db.add_argument("--csv", action="store_true", help="Output CSV instead of a table")

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


def cmd_offers(args: argparse.Namespace, settings: Any) -> int:
    """Find offers in the already-scraped data and render their tweets."""
    from affiliate import AffiliateError, build_link_from_settings
    from lib.log import log_ok, log_stage, log_step, log_warn
    import offers as off
    import tweets as tw

    store = _get_store(settings)
    try:
        log_stage("Checking the scraped data")
        bail = _require_fresh_data(store, settings, args)
        if bail is not None:
            return bail

        stored = store.latest_matched_products()
        matched = off.match_products(stored, off.load_keywords(settings), settings)
        cooldown = store.recently_posted(settings.repost_cooldown_days)
        deals = off.filter_offers(matched, settings, exclude_ids=cooldown)

        log_stage(f"{len(deals)} offer(s) worth posting")
        if not deals:
            log_warn("Nothing cleared the thresholds. Lower min_discount_pct in "
                     "config.json, widen the keywords, or run ./run ingest again.")
            return 0

        untagged = False
        rendered: list[tuple[Any, str, str]] = []
        for product in deals[: args.limit]:
            try:
                link = build_link_from_settings(product.url, settings)
            except AffiliateError as e:
                if args.post:
                    log_err(str(e))
                    return 1
                if not untagged:
                    log_warn(f"{e}\n  Previewing with untagged links.")
                    untagged = True
                link = product.url
            text = tw.build_tweet(product, link, settings, deterministic=not args.llm)
            rendered.append((product, link, text))

        for i, (product, link, text) in enumerate(rendered, 1):
            print()
            print(f"\033[1m─── {i}/{len(rendered)}  "
                  f"{product.discount_pct}% OFF · {product.matched_label} · "
                  f"{tw.tweet_length(text, link)}/{tw.TWEET_LIMIT} chars\033[0m")
            print(text)

        print()
        if not args.post:
            log_ok(f"{len(rendered)} tweet(s) generated (preview only)")
            log_step("Publish them with:  ./run offers --post --limit N")
            return 0

        return _publish(rendered, store, settings)
    finally:
        store.close()


def _publish(rendered: list[tuple[Any, str, str]], store: Any, settings: Any) -> int:
    """Send already-rendered tweets, recording each one."""
    from lib.log import log_err, log_ok, log_stage
    from lib.twitter_post import TwitterPoster, TwitterPostError

    log_stage(f"Posting {len(rendered)} tweet(s)")
    poster = TwitterPoster(cache_dir=cfg.STATE_DIR, dry_run=False)

    for i, (product, link, text) in enumerate(rendered):
        try:
            tweet_id = poster.post(text)
        except TwitterPostError as e:
            log_err(f"post failed: {e}")
            return 1
        store.record_post(
            product_id=product.product_id, tweet_id=tweet_id, tweet_text=text,
            affiliate_url=link, price=product.price,
            discount_pct=product.discount_pct, dry_run=False,
        )
        log_ok(f"posted https://x.com/i/status/{tweet_id}")
        if i < len(rendered) - 1:
            time.sleep(settings.delay_between_tweets_sec)

    log_ok(f"{len(rendered)} tweet(s) posted")
    return 0


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
        """SELECT posted_at AS at, tweet_id, substr(tweet_text,1,60) AS text,
                  price, discount_pct AS pct
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
        "offers": cmd_offers,
        "db": cmd_db,
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
