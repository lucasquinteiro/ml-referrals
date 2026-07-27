# ml-referrals

Finds discounted products on Mercado Libre, records their prices over time, and
tweets the good ones with your affiliate link.

Everything is driven by a keyword list in [`config.json`](config.json) — that's
the one file you edit day to day.

```
keywords (config.json)
        │
        ▼
   ./run ingest ──► scrape ──► match titles ──► snapshot prices
                                                │
                                                ▼
   ./run simulate ──► filter by discount ──► affiliate link ──► preview
                                                │
   ./run post ──────────────────────────────────┴──► 1 tweet
```

## Why it scrapes `/ofertas` and not search

No official API is used (it requires an app + token that don't work for this).
The obvious target would be `listado.mercadolibre.com.ar/<keyword>`, but as of
July 2026 Mercado Libre Argentina puts search listings behind a login wall
(*"Para continuar, ingresá a tu cuenta"*) plus a JS bot challenge — that's true
even in a normal browser with a clean session.

`/ofertas` has none of that. It renders for anonymous visitors, and it's a
better source anyway: it *is* the discounted catalogue (~10k products), paginated
at ~45 cards per page, with list price, sale price and discount already on each
card. So the pipeline crawls N pages of `/ofertas` and matches titles against
your keywords locally. (The page's own `?q=` parameter is silently ignored by
Mercado Libre — filtering has to happen on our side.)

Pages are rendered in a real headless Chromium via Playwright, so the site's own
JS runs exactly as it does for a normal visitor. Requests are spaced out by
`delay_between_pages_sec`; please leave that at a polite value.

### Optional: scraping search with a logged-in session

`/ofertas` only shows what Mercado Libre itself flags as discounted. Search
covers the **whole catalogue**, which is what you want for tracking specific
products' prices over time and calling deals yourself. That needs a session:

```bash
./run login                              # opens a browser; you log in yourself
./run ingest --source search --pages 2   # searches each configured keyword
```

`./run login` opens a real browser window and waits. You type your credentials
into Mercado Libre's own page — nothing passes through this code, and it verifies
the session works before saving it. Playwright then writes the cookies to
`state/ml-storage.json` (gitignored). Re-run it when the session expires.

Set `ML_STORAGE_STATE_PATH` to point at a session file somewhere else.

> **Think about which account you use.** This drives an automated, logged-in
> session against a wall Mercado Libre put up deliberately, so there's a real
> chance of the account getting rate-limited or flagged. If that's the same
> account your affiliate earnings are tied to, that's your business at risk —
> prefer a separate account. Also note this doesn't transfer to GitHub Actions
> cleanly: a datacenter IP plus a logged-in session is a much stronger bot
> signal than your home connection, and a headless run can't answer a re-auth
> or 2FA prompt. `/ofertas` needs none of this, which is why it's the default.

Category pages (`/c/<category>`) are *not* a way around this — they're browse
landing pages: no discount data, and `?page=2` returns the same items.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env      # then fill it in
```

### Affiliate setup (the one thing you must do to publish)

**There is no Mercado Libre affiliate API.** Their public API
([developers.mercadolibre.com](https://developers.mercadolibre.com.ar/)) covers
items, search, orders, shipping and users — there is no endpoint that mints
affiliate links, and the affiliate program is dashboard-only.

You don't need one. Attribution is carried by two query params that are **the
same for every product you ever promote**:

```
?matt_word=<your tag>&matt_tool=<your tool id>
```

So you generate **one** link by hand, and this project applies your identity to
unlimited product URLs from then on. That's the automation — there's nothing
per-link to fetch.

1. Join at **[mercadolibre.com.ar/afiliados](https://www.mercadolibre.com.ar/afiliados)**
   (needs a Mercado Libre account; you'll get a confirmation mail and have to
   activate your social profile).
2. Generate a link for any product — from the affiliate dashboard, or the
   "Compartir / Generar link de afiliado" button that appears on product pages
   once you're approved.
3. Paste it here:

```bash
./run set-affiliate "https://mercadolibre.com/sec/1AbCdEf"
```

That parses out your tag and tool id — following the redirect if it's a short
`/sec/` link — and writes them to `.env`:

```
✓ tag  (matt_word): lucasq
✓ tool (matt_tool): 68232872
✓ written to .env (gitignored)

  every link will now look like:
    https://www.mercadolibre.com.ar/producto/p/MLA12345678?matt_word=lucasq&matt_tool=68232872&forceInApp=true
```

Existing keys in `.env` are preserved. Sanity-check any product URL with
`./run check-affiliate <url>`.

> Compare that generated link against the one your dashboard gave you. If your
> account produces a different shape (some get a `/social/<tag>` wrapper), set
> `affiliate.link_template` in `config.json` — `{url}`, `{url_encoded}`,
> `{tag}` and `{tool}` are substituted. **Attribution fails silently if this is
> wrong**, so it's worth one click through a test link to confirm the sale
> registers.

`post` refuses to publish without a tag configured — an untagged link is lost
commission. `simulate` still works (it warns and previews with a plain URL).

### Credentials (`.env`)

| Variable | Needed for | Where it comes from |
| --- | --- | --- |
| `ML_AFFILIATE_TAG` | posting | set by `./run set-affiliate` (see above) |
| `ML_AFFILIATE_TOOL_ID` | posting | set by `./run set-affiliate` (see above) |
| `TWITTER_AUTH_TOKEN` | posting | `auth_token` cookie from a logged-in x.com session |
| `TWITTER_CT0` | posting | `ct0` cookie from the same session |
| `GROQ_API_KEY` | nicer tweet copy (optional) | console.groq.com |
| `SUPABASE_*` | only when `store: "supabase"` | Supabase project settings |

Twitter auth is the **same cookie approach the other pipelines use** — no
official API, no developer app. If `../twitter-updates/.env` or a
`twitter-cookies.json` already exists locally, this project picks it up
automatically, so you may not need to set anything.

## Usage

The everyday loop is three commands — **scrape**, **check**, **publish**:

```bash
./run ingest      # scrape + record price snapshots
./run simulate    # exactly what the next post would publish — writes nothing
./run post        # publish that one tweet
```

`simulate` is the dry run: it renders the single tweet that `post` would send,
then lists what's queued behind it, and touches nothing.

```
─── WOULD POST NEXT · 72% OFF · Monitores · 179/280 chars
💥 Monitor Led 27 Oasis 75hz Full Hd 1920x1080 Hdmi/vga Negro

🏷️ Antes $600.000
🔥 Ahora $169.900 (72% OFF)
💰 Ahorro: $430.100

https://www.mercadolibre.com.ar/monitor-led-27-...

#Ofertas · Link de afiliado

  then, on the following runs (295 more queued):
    2. 71% off — Cafetera Express 033 Dyvan Espumador 20 Bar (Cafeteras)
    3. 70% off — Auriculares Inalámbricos FETUZZ de Oído Abierto (Auriculares)

  ✓ simulation only — nothing posted, nothing written to the store
```

**Posting is always one tweet per run.** There's no `--limit` on `post`: a burst
of affiliate links reads as spam, and one at a time keeps every publish
reviewable. Run it again for the next one — the queue is ordered by discount,
and anything already posted is held back for `repost_cooldown_days`.

To see more of the queue than `simulate` shows, use `./run offers --limit 20`.

These commands read the stored data — they never scrape. If the data is older
than `max_data_age_hours` (default 12) they stop and tell you to run `./run
ingest`, because a tweet built from an expired price is worse than no tweet:

```
✗ Scraped data is 19.4h old (limit 12h) — prices have probably moved
  and some offers will have expired.
  Refresh it with:
      ./run ingest
  ...or pass --stale-ok to use it anyway.
```

**Copy is deterministic.** The template is chosen from a hash of the product id,
so the same product always renders byte-identical text — re-run it as often as
you like and review before publishing. Pass `--llm` for LLM-written copy
instead (varied, but not reproducible).

Everything else:

```bash
./run login                     # optional: unlock search scraping
./run ingest --dry-run          # scrape + match, write nothing
./run offers --limit 20         # browse the whole queue
./run db --name offers          # query the store (see below)
./run report                    # summary of what's in the store
./run set-affiliate <link>      # configure your affiliate tag
./run check-affiliate <url>     # verify your affiliate link shape
./run post --ingest             # scrape fresh, then post from that
```

Useful flags: `ingest --pages N`, `ingest --headed` (watch the browser),
`ingest --keyword notebook` (override config), `ingest --source search` (needs
login), `ingest --all` (snapshot every scraped product, not just keyword
matches), `simulate --queue 10`, and `--stale-ok` / `--max-age-hours` /
`--llm` on all three of `simulate`, `offers` and `post`.

## Querying the database

`./run db` opens the SQLite store **read-only**, so a typo can't damage your
price history. With no arguments it lists the built-in queries:

```bash
./run db                        # list the named queries
./run db --name offers          # best current discounts
./run db --name movers          # products whose price actually changed
./run db --name history         # full price history, newest first
./run db --name categories      # how many products matched each keyword
./run db --name posted          # tweets already sent
./run db --name runs            # ingest runs
./run db --name stale           # how old the data is
```

Any SQL works too, and `--csv` makes it pipeable:

```bash
./run db "SELECT title, price FROM products p
          JOIN price_history h USING (product_id) ORDER BY h.price DESC LIMIT 10"

./run db --name history --csv > history.csv
```

Tables are `products`, `price_history`, `posts` and `runs` — schema in
[`store.py`](store.py). Or use `sqlite3` directly:

```bash
sqlite3 state/ml_referrals.db "SELECT COUNT(*) FROM price_history"
```

## Configuring what to look for

`keywords` is the heart of it. An entry is either a plain string or an object:

```json
{
  "term": "notebook",
  "label": "Notebooks",
  "min_discount_pct": 25,
  "exclude": ["soporte", "cooler", "mochila"]
}
```

- `term` — matched against the product title. With the default
  `keyword_match_mode: "any_word"`, every word must appear somewhere in the
  title (so `"samsung galaxy"` also matches *"Galaxy A54 Samsung Liberado"*).
  Set it to `"phrase"` for strict substring matching.
- `exclude` — words that disqualify a match. Essential for filtering accessories
  out of broad terms.
- `min_discount_pct` / `min_price` / `max_price` — per-keyword overrides of the
  top-level thresholds.

`global_exclude` applies to every keyword (`funda`, `repuesto`, `replica`, …).

Matching is accent- and case-insensitive, so `"television"` matches *"Televisión"*.

Other settings worth knowing: `min_discount_pct` (global floor),
`repost_cooldown_days` (don't re-post the same product for N days),
`max_data_age_hours` (staleness limit for `simulate`/`offers`/`post`),
`pages_per_run`, `use_llm_for_copy`, `tweet_disclosure`.

## Price history

Every ingest appends one row per product to `price_history` — price, list price,
discount, badge, rating, timestamp. **No analysis is done on it yet**, on
purpose: the table needs depth before "cheapest in 90 days" means anything.
Once there's a few weeks of data, that check slots in as a third stage in
`offers.py`, after matching and filtering.

Export it any time with `./run report --export prices.json`.

## Storage backends

- `"store": "sqlite"` (default) — `state/ml_referrals.db`, gitignored. Fine for
  local runs.
- `"store": "supabase"` — required for GitHub Actions, because a runner is
  ephemeral and a local SQLite file would be discarded after every run. Apply
  [`supabase_schema.sql`](supabase_schema.sql) once, set `SUPABASE_URL` and
  `SUPABASE_SERVICE_ROLE_KEY`, and flip the setting. Tables are `mlr_`-prefixed
  since the Supabase project is shared with the other pipelines.

## Tweets

Copy comes from the LLM when a key is available, falling back to templates
otherwise (templates always work — the fallback is automatic, not a failure).
Length is budgeted the way X counts it: any URL weighs 23 characters regardless
of its real length.

`tweet_disclosure` appends an affiliate disclosure to every tweet. Keep it —
disclosing affiliate links is required by X's rules and by consumer-protection
law in most jurisdictions.

Posting goes through X's own web GraphQL `CreateTweet` endpoint with cookie
auth. Two things there drift over time and both self-heal: the GraphQL query id
is discovered from X's JS bundle and cached in `state/`, and missing `features`
flags are re-added from X's own error response and retried once.

## GitHub Actions

Two workflows, deliberately separate so scraping and posting cadence are
independent:

- [`ingest.yml`](.github/workflows/ingest.yml) — 4×/day, builds price history.
- [`post.yml`](.github/workflows/post.yml) — 2×/day, tweets from the latest ingest.

Both also run via **workflow_dispatch** with dry-run inputs, which is the safest
way to try them the first time.

Before enabling: set `store` to `"supabase"` in `config.json`, and add these
repository secrets — `ML_AFFILIATE_TAG`, `ML_AFFILIATE_TOOL_ID`,
`TWITTER_AUTH_TOKEN`, `TWITTER_CT0`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, and optionally `GROQ_API_KEY`.

## Layout

| File | Role |
| --- | --- |
| `config.json` / `config.py` | settings + keyword list |
| `scraper.py` | Playwright crawler over `/ofertas`, card parsing |
| `offers.py` | keyword matching + deal filtering |
| `affiliate.py` | `matt_word` / `matt_tool` link building |
| `store.py` / `supabase_store.py` | price history, posts, runs |
| `tweets.py` | tweet copy (LLM + templates) |
| `lib/twitter.py` | cookie loading, vendored from `twitter-updates` |
| `lib/twitter_post.py` | `CreateTweet` via cookie auth |
| `run.py` | CLI |

## Caveats

- **Verify your affiliate link shape once.** `./run check-affiliate <url>`
  produces `?matt_word=<tag>&matt_tool=<tool>`. Compare it against a link
  generated in your dashboard; if yours differs, set `affiliate.link_template`
  in `config.json` (`{url}`, `{url_encoded}`, `{tag}`, `{tool}` are
  substituted). Attribution silently fails if this is wrong.
- Scraping breaks when Mercado Libre changes their markup. The run warns when it
  scrapes fewer than `min_products_expected` products — that's the signal to
  check the selectors in `scraper.py`.
- X cookies expire. When posting starts returning 403, re-export `auth_token`
  and `ct0`.
