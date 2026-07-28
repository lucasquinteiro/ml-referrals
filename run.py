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
    ing.add_argument("--no-slack", action="store_true",
                     help="Skip the Slack summary even if SLACK_WEBHOOK_URL is set")

    lg = sub.add_parser(
        "login",
        help="Save a Mercado Libre session (scraping burner or affiliate account)",
    )
    lg.add_argument("--role", choices=["scraping", "affiliate"], default="scraping",
                    help="scraping = burner for search pages (default); "
                         "affiliate = the account that mints your links")

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
    log_step(f"scraping session: {auth.describe_session(auth.SCRAPING)}")

    if source == "search" and not auth.has_session(auth.SCRAPING):
        raise SystemExit(
            "Search needs a logged-in Mercado Libre session. Run "
            "`./run login --role scraping` first (use a burner account), or "
            "drop --source search to use /ofertas, which needs no login."
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

        # Queue depth reflects everything stored, not just this run's haul.
        queue_total = len(_select_deals(store, settings))
    finally:
        store.close()

    if not args.no_slack:
        import notifier

        notifier.notify(notifier.build_summary(
            site=settings.site, source=args.source, pages=pages,
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


def _select_deals(
    store: Any, settings: Any, *, min_discount: Optional[int] = None
) -> list[Any]:
    """The postable queue: keyword-matched, threshold-clearing, off cooldown.

    Ordered best-discount-first, so the head of this list is always the next
    thing that would go out.
    """
    import offers as off

    stored = store.latest_matched_products()
    matched = off.match_products(stored, off.load_keywords(settings), settings)
    cooldown = store.recently_posted(settings.repost_cooldown_days)
    return off.filter_offers(
        matched, settings, exclude_ids=cooldown, min_discount_override=min_discount
    )


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
                    [p.url for p in missing], site=settings.site, tag=tag
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
    print(text)


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

        links = _resolve_links(deals[:1], settings, store, allow_untagged=True)
        if links is None:
            return 1
        link = links[deals[0].product_id]
        text = _render(deals[0], link, settings, use_llm=args.llm)
        _show(deals[0], link, text, "WOULD POST NEXT", source=True)

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
    """Publish exactly one tweet: the best offer currently in the queue.

    One at a time, always. A burst of affiliate links reads as spam, and a
    single post per run keeps every publish reviewable — run `./run simulate`
    first to see precisely what this will send.
    """
    from lib.log import log_err, log_ok, log_stage, log_step, log_warn
    from lib.twitter_post import TwitterPoster, TwitterPostError

    store = _get_store(settings)
    try:
        if args.ingest:
            pages = args.pages or settings.pages_per_run
            _, matched = _scrape_and_match(settings, pages=pages, headed=False)
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

        product = deals[0]
        links = _resolve_links([product], settings, store,
                               allow_untagged=args.dry_run)
        if links is None:
            return 1
        link = links[product.product_id]
        text = _render(product, link, settings, use_llm=args.llm)
        _show(product, link, text, "DRY RUN" if args.dry_run else "POSTING")

        print()
        try:
            tweet_id = TwitterPoster(cache_dir=cfg.STATE_DIR,
                                     dry_run=args.dry_run).post(text)
        except TwitterPostError as e:
            log_err(f"post failed: {e}")
            return 1

        store.record_post(
            product_id=product.product_id, tweet_id=tweet_id, tweet_text=text,
            affiliate_url=link, price=product.price,
            discount_pct=product.discount_pct, dry_run=args.dry_run,
        )
        if tweet_id:
            log_ok(f"posted https://x.com/i/status/{tweet_id}")
            log_step(f"{len(deals) - 1} offer(s) still queued for the next run")
        else:
            log_ok("dry run — nothing published")
        return 0
    finally:
        store.close()


def cmd_report(args: argparse.Namespace, settings: Any) -> int:
    from lib.log import log_ok, log_stage, log_step

    import auth

    store = _get_store(settings)
    try:
        log_stage("Mercado Libre sessions")
        for role in auth.ROLES:
            log_step(f"{role:10} {auth.describe_session(role)}")

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

    return auth.login(settings.site, args.role)


def main() -> int:
    args = _parse_args()
    cfg.bootstrap()
    settings = cfg.load_settings()

    handlers = {
        "login": cmd_login,
        "ingest": cmd_ingest,
        "simulate": cmd_simulate,
        "offers": cmd_offers,
        "db": cmd_db,
        "post": cmd_post,
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
