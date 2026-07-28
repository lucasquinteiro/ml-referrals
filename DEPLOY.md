# Running this on GitHub Actions

**Only posting is scheduled.** Everything that touches Mercado Libre runs on
your own machine.

| Where | Job | Touches Mercado Libre? |
| --- | --- | --- |
| **GitHub, every 3h** | [`post.yml`](.github/workflows/post.yml) — one tweet | **No** |
| Local, when you choose | `./run ingest` — scrape + snapshot prices | yes, anonymous |
| Local, when you choose | link minting (part of ingest) | yes, affiliate account |
| Local, when you choose | `./run images` — capture offer cards | yes, anonymous |

That split is deliberate. A datacenter IP hitting Mercado Libre on a fixed
schedule is what gets IPs and accounts flagged, and every flag costs a re-login
on an account you can't afford to keep re-authenticating. So the scheduled job
only ever talks to X and Supabase — it reads what your local runs stored.

[`ingest.yml`](.github/workflows/ingest.yml) still exists but is **not
scheduled**: manual dispatch only, anonymous, and it never mints links.

## The local loop

**Sessions are the scarce resource.** Both of ours died within a day, and a
large search crawl looks like part of what kills them. So don't log in per
operation — log in once and take everything in that window:

```bash
./run login --role scraping    # burner, only when the last session died
./run harvest                  # search-scrape + capture offer cards
./run links                    # separately: mint affiliate links, paced
```

`harvest` scrapes and captures cards. It deliberately does **not** mint
affiliate links.

Scraping is anonymous and cheap to repeat. Minting is authenticated activity on
the account your commissions belong to, and a long unbroken run of link
creations is the pattern most likely to get that account looked at — so it
never rides along with a scrape. `./run links` mints in batches of 5 with a
~25s pause between them, capped at 10 per run
(`affiliate.link_batch_size`, `delay_between_link_batches_sec`,
`max_links_per_ingest`). Check what it would do first:

```bash
./run links --dry-run
```

Links are cached per (product, tag), so a product only ever costs one
creation. Afterwards the posting
workflow runs off the database for days without touching Mercado Libre, so a
dead session costs you nothing until the next harvest.

For the anonymous path, which needs no session at all and can run whenever:

```bash
./run ingest --pages 4                 # /ofertas, anonymous
./run ingest --pages 4 --start-page 5  # later: the next chunk, not one burst
./run images --limit 10                # cards from /ofertas, anonymous
./run simulate                         # see what the next tweet will be
```

Page fetches are spaced by `delay_between_pages_sec` (4s) randomised +/-50%, so
the interval isn't itself a signature. `--start-page` splits a big crawl across
several runs spread over the day.

`./run images` renders Mercado Libre's real offer card. It needs a browser, so
it stays local; the PNG goes to Supabase Storage and the posting job just
downloads it. `--source ofertas` (default) is anonymous; `--source search`
covers products ML doesn't flag as offers and needs the scraping session —
which is why `harvest` does it inside the same login window.

There is no anonymous route to search listings. Verified: the official API
returns `403 forbidden`, and `listado.`, `m.` and product pages all redirect to
the login wall. If you want the catalogue beyond ML's own offers page, a
session is the price.

---

## 1. Supabase (required)

A GitHub runner is ephemeral: a SQLite file under `state/` is thrown away when
the job ends, so the price history would never accumulate and `post` would have
nothing to read. Supabase gives you a database without hosting anything.

1. Create a project at [supabase.com](https://supabase.com) (free tier is fine).
2. Open **SQL Editor** and run the contents of
   [`supabase_schema.sql`](supabase_schema.sql). It creates five `mlr_`-prefixed
   tables (the prefix keeps it safe to share a project with other pipelines).
3. If you want `./run images`, create a **public** Storage bucket named
   `offer-cards` (Storage → New bucket → public). Skip it and tweets fall back
   to the plain product photo.
4. **Settings → API** gives you the two values you need:
   - Project URL → `SUPABASE_URL`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY`
5. Flip the backend in [`config.json`](config.json):

```json
"store": "supabase"
```

> Use the **service_role** key, not the anon key — the anon key is blocked by
> row-level security. It's a full-access credential: repository secrets only,
> never committed.

Test it locally before trusting a scheduled run:

```bash
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... ./run ingest --pages 2
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... ./run simulate
```

`./run db` only works against SQLite; query Supabase from its own SQL editor.

---

## 2. Repository secrets

**Settings → Secrets and variables → Actions → New repository secret.**

### Required to post

| Secret | Value |
| --- | --- |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase `service_role` key |
| `ML_AFFILIATE_TAG` | your affiliate tag (e.g. `sharkdeals`) — **required**, see below |
| `ML_AFFILIATE_TOOL_ID` | the `matt_tool` number — only used by the fallback |
| `TWITTER_AUTH_TOKEN` | `auth_token` cookie from a logged-in x.com session |
| `TWITTER_CT0` | `ct0` cookie from that same session |

Both are the values `./run set-affiliate` wrote to your local `.env`.

**Why the tag is required:** it's a required field of the link builder request
(`{"urls": [...], "tag": "sharkdeals"}`) — Mercado Libre needs to know which
affiliate to credit, and nothing in the session says that, since one account can
hold several tags (`./run affiliate-tags`). No tag means no link generated, and
the fallback form needs it too. No tag anywhere = untagged links = no commission.

**Why the tool id is still worth setting:** the link builder embeds it in the
signed link, so it's unused while that works. It only feeds the
`?matt_word=…&matt_tool=…` fallback — which is exactly what you get when the
session expires, so it's the thing keeping links tracked until you notice.

Neither is a secret in any real sense: both appear in every link you publish.
They're listed as secrets for tidiness, but `affiliate.tag` / `affiliate.tool_id`
in `config.json` work identically if you'd rather commit them.

### Optional but recommended

| Secret | Effect if missing |
| --- | --- |
| `SLACK_WEBHOOK_URL` | no ingest summary posted |
| `ML_AFFILIATE_STORAGE_STATE` | links fall back to `?matt_word=…` instead of `meli.la/…` |
| `GROQ_API_KEY` | template copy instead of LLM copy |

**There is no scraping-session secret**, and that isn't the two-account split
breaking down — it's the split working. Which account runs where:

| Where | What it does | Account |
| --- | --- | --- |
| CI `ingest` | scrapes `/ofertas` | **none** — the page is public |
| CI `post` | mints `meli.la` links | affiliate |
| Local `ingest --source search` | search pages behind the wall | **burner** |
| Local `simulate` / `post` | mints links | affiliate |

The burner is still doing its job; it's just only needed for `--source search`,
which stays local. The risky activity — hammering search pages — never leaves
your home IP, and the only thing CI ever authenticates as is the affiliate
account, doing a handful of link-builder calls that look nothing like scraping.

If you *do* want search coverage from CI, add `ML_STORAGE_STATE` (the contents
of `state/ml-storage.json`) and pass `--source search` in the workflow. Read the
warning above first: that puts your burner's logged-in session on a datacenter
IP every day, which is the fastest way to get it flagged.

### The affiliate session secret

`ML_AFFILIATE_STORAGE_STATE` is the **entire contents** of your local session
file — Actions has no browser to log in with, so the session has to be carried
in. Easiest from the terminal, which never puts the value on a clipboard or a
web form:

```bash
gh secret set ML_AFFILIATE_STORAGE_STATE < state/ml-session-affiliate.json
```

Or paste the file contents into the web UI as the secret value.

> **These expire.** When they do, link generation silently falls back to the
> `matt_word` form (still tracked, just not a short link) and search scraping
> starts hitting the login wall. Re-run `./run login --role <role>` locally and
> update the secret. There's no way around this: a headless CI run can't answer
> a re-auth or 2FA prompt.

### Getting the Twitter cookies

In a logged-in x.com tab: DevTools → Application → Cookies → `https://x.com`,
copy the `auth_token` and `ct0` values. Same pair the `twitter-updates` project
uses — if it's already working there, reuse those.

---

## 3. Slack

Create an [Incoming Webhook](https://api.slack.com/messaging/webhooks) for the
channel you want, and set the URL as `SLACK_WEBHOOK_URL`. Each ingest posts:

```
🛒 ml-referrals — ingest
ofertas · 12 page(s) · mercadolibre.com.ar

888 scraped → 794 matched a keyword → 296 clear the thresholds
Monitores: 98 · Aire acondicionado: 94 · Cafeteras: 92 · Smart TV: 86 …

Top 5 deals:
• 72% OFF — Monitor Led 27 Oasis 75hz Full Hd 1920x1080 Hdmi/vga Negro
   $600.000 → $169.900  ·  Monitores
```

Notifications never fail a run — an unreachable Slack is logged and ignored.
`./run ingest --no-slack` skips it locally.

---

## 4. Turn it on

Scheduled workflows only run from the **default branch**, and GitHub disables
schedules in repos with no activity for 60 days.

Do a manual run of each first, in dry-run mode:

**Actions → ingest → Run workflow → dry_run: true**, then the same for **post**.
That exercises the real secrets without writing data or publishing anything.

Then let the schedules take over. `./run report` locally (pointed at Supabase)
shows what the scheduled runs have been doing.

---

## Things worth knowing

**Timing.** `max_data_age_hours` in `config.json` is `26`. That's deliberate:
with a daily ingest, the last posts of the day read prices ~24h old, and
anything at or below 24 would make them refuse to post. If you move ingest to a
different cadence, move this too.

**Search scraping doesn't really work in CI.** A datacenter IP plus a logged-in
session is a much stronger bot signal than your home connection. The workflow
uses `/ofertas`, which needs no login at all. If you want search coverage, run
`./run ingest --source search` locally against Supabase and let the scheduled
runs top up from `/ofertas`.

**Posting cadence.** 8 tweets/day from personal session cookies is a reasonable
load. If posts start failing with 403, the cookies expired — re-export them. Go
gentler by widening the cron (`0 */6 * * *` = 4/day) rather than by touching
the code; `post` sends one either way.

**The queue drains.** `repost_cooldown_days` (21) holds a product back after
posting, and at 8/day a 296-deep queue lasts about five weeks. The 60%+ tier is
much thinner — 10 right now, so roughly a day — which is why runs tell you when
they've dropped below `preferred_min_discount_pct`. Seeing that regularly means
it's time to widen keywords, scrape more pages, or accept a lower tier.
