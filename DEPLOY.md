# Running this on GitHub Actions

Two scheduled workflows, no server to host:

| Workflow | Schedule | What it does |
| --- | --- | --- |
| [`ingest.yml`](.github/workflows/ingest.yml) | daily, 11:00 UTC (08:00 ART) | scrape, snapshot prices, Slack summary |
| [`post.yml`](.github/workflows/post.yml) | every 3 hours | publish exactly one tweet |

`post` always sends one tweet, so the cron *is* the rate limit — every 3 hours
means 8 tweets/day.

---

## 1. Supabase (required)

A GitHub runner is ephemeral: a SQLite file under `state/` is thrown away when
the job ends, so the price history would never accumulate and `post` would have
nothing to read. Supabase gives you a database without hosting anything.

1. Create a project at [supabase.com](https://supabase.com) (free tier is fine).
2. Open **SQL Editor** and run the contents of
   [`supabase_schema.sql`](supabase_schema.sql). It creates five `mlr_`-prefixed
   tables (the prefix keeps it safe to share a project with other pipelines).
3. **Settings → API** gives you the two values you need:
   - Project URL → `SUPABASE_URL`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY`
4. Flip the backend in [`config.json`](config.json):

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
| `ML_AFFILIATE_TAG` | your affiliate tag (e.g. `sharkdeals`) |
| `ML_AFFILIATE_TOOL_ID` | the `matt_tool` number |
| `TWITTER_AUTH_TOKEN` | `auth_token` cookie from a logged-in x.com session |
| `TWITTER_CT0` | `ct0` cookie from that same session |

`ML_AFFILIATE_TAG` / `ML_AFFILIATE_TOOL_ID` are the two values `./run
set-affiliate` wrote to your local `.env` — copy them from there.

### Optional but recommended

| Secret | Effect if missing |
| --- | --- |
| `SLACK_WEBHOOK_URL` | no ingest summary posted |
| `ML_AFFILIATE_STORAGE_STATE` | links fall back to `?matt_word=…` instead of `meli.la/…` |
| `GROQ_API_KEY` | template copy instead of LLM copy |
| `ML_STORAGE_STATE` | only needed for `--source search` |

### The session secrets

`ML_AFFILIATE_STORAGE_STATE` is the **entire contents** of your local session
file — Actions has no browser to log in with, so the session has to be carried
in:

```bash
cat state/ml-session-affiliate.json | pbcopy
```

Paste that as the secret value. `ML_STORAGE_STATE` works the same way for
`state/ml-session-scraping.json`.

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
