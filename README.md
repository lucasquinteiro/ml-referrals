# ml-referrals

Finds discounted products on Mercado Libre, records their prices over time, and
tweets the good ones with your affiliate link.

Everything is driven by a keyword list in [`config.json`](config.json) — that's
the one file you edit day to day.

```
keywords (config.json)
        │
        ▼
   scrape /ofertas ──► match titles ──► filter by discount ──► snapshot prices
                                                │
                                                ▼
                                       affiliate link ──► tweet
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

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env      # then fill it in
```

### Credentials (`.env`)

| Variable | Needed for | Where it comes from |
| --- | --- | --- |
| `ML_AFFILIATE_TAG` | posting | Programa de Afiliados dashboard (`matt_word`) |
| `ML_AFFILIATE_TOOL_ID` | posting | same dashboard (`matt_tool`) |
| `TWITTER_AUTH_TOKEN` | posting | `auth_token` cookie from a logged-in x.com session |
| `TWITTER_CT0` | posting | `ct0` cookie from the same session |
| `GROQ_API_KEY` | nicer tweet copy (optional) | console.groq.com |
| `SUPABASE_*` | only when `store: "supabase"` | Supabase project settings |

Twitter auth is the **same cookie approach the other pipelines use** — no
official API, no developer app. If `../twitter-updates/.env` or a
`twitter-cookies.json` already exists locally, this project picks it up
automatically, so you may not need to set anything.

## Usage

```bash
./run ingest --dry-run          # scrape + match, write nothing
./run ingest                    # scrape + record price snapshots
./run post --dry-run            # show the tweets that would go out
./run post --limit 1            # actually tweet one
./run report                    # what's in the store
./run check-affiliate <url>     # verify your affiliate link shape
```

Useful flags: `ingest --pages N`, `ingest --headed` (watch the browser),
`ingest --keyword notebook` (override config), `ingest --all` (snapshot every
scraped product, not just keyword matches), `post --ingest` (scrape fresh
instead of using the last stored run).

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
`tweets_per_run`, `pages_per_run`, `use_llm_for_copy`, `tweet_disclosure`.

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
