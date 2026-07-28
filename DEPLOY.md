# Deploying ml-referrals

Runs on a single always-on box (a DigitalOcean droplet). Not GitHub Actions —
an ephemeral runner can't keep a Mercado Libre session warm, can't hold the rate
gate's state across runs, and puts a rotating datacenter IP in front of ML. A
persistent server fixes all three.

**Full walkthrough: [`deploy/README.md`](deploy/README.md).** The short version:

```bash
# on the droplet
git clone https://github.com/lucasquinteiro/ml-referrals /opt/ml-referrals
bash /opt/ml-referrals/deploy/setup.sh

# from your laptop
bash deploy/push.sh root@your-droplet
```

Three systemd timers then run it: `ingest` (4×/day, search listings), `post`
(every 3h), `session-check` (2×/day). Everything that touches Mercado Libre goes
through the rate gate, so nothing bursts.

## Why it stays safe

- **One login per account.** Sessions are created on your laptop and rsynced up.
  `session-check` Slacks you the day one dies; until then a dead session only
  degrades (untagged links, product photos), never drops a tweet.
- **Nothing happens all at once.** The gate (`config.json` → `ml_gate`) enforces
  a jittered minimum interval, per-account hourly/daily budgets, and a
  circuit-breaker cooldown on any wall — shared across every process.
- **Post-time generation.** Each post mints its link and captures its card
  lazily and cached, so a post is at most two metered ML requests, 3h apart.

## Storage

Supabase (default) or SQLite. On a single droplet SQLite is simpler — everything
is on one disk — but Supabase lets you query and run things from elsewhere too.

- **Supabase:** apply [`supabase_schema.sql`](supabase_schema.sql) in the SQL
  editor, and (for card images) create a public `offer-cards` Storage bucket.
  Set `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env`.
- **SQLite:** set `"store": "sqlite"` in `config.json` (or `ML_STORE=sqlite`).
  The db lives in `state/` and `push.sh` carries it up.

## `.env`

| Variable | For |
| --- | --- |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | store (if Supabase) |
| `ML_AFFILIATE_TAG`, `ML_AFFILIATE_TOOL_ID` | affiliate links (`./run set-affiliate`) |
| `TWITTER_AUTH_TOKEN`, `TWITTER_CT0` | posting to X |
| `SLACK_WEBHOOK_URL` | ingest summaries + dead-session alerts |
| `GROQ_API_KEY` | LLM tweet copy (optional; templates otherwise) |

Sessions aren't env vars — they're the `state/ml-session-*.json` files (or
`state/profiles/` with `--persist`), created by `./run login` and rsynced up.

## Before the first deploy

- [ ] `./run set-affiliate "<link>"` — affiliate tag in `.env`
- [ ] Twitter `auth_token` + `ct0` in `.env`
- [ ] `./run login --role scraping` (burner) and `--role affiliate` on your laptop
- [ ] `./run session-check` green locally
- [ ] store chosen (Supabase schema applied, or `store: sqlite`)
