# Deploying ml-referrals on a droplet

Everything runs on one always-on box. No GitHub Actions, no ephemeral runners —
a persistent server is what lets Mercado Libre sessions stay warm and lets the
rate gate hold state across runs.

Three systemd timers do the work:

| Timer | Cadence | What runs | Touches ML |
| --- | --- | --- | --- |
| `ml-referrals-ingest` | 4×/day | `ingest --source search` | yes — throttled trickle |
| `ml-referrals-post` | 5×/day | `post` | ≤2 gated requests |
| `ml-referrals-session-check` | 2×/day | `session-check` | 1 per session |

Everything ML-facing routes through the rate gate (`config.json` → `ml_gate`),
so nothing here can burst regardless of when the timers land.

## First deploy

**1. On the droplet** (Ubuntu 22.04+), as root:

```bash
git clone https://github.com/lucasquinteiro/ml-referrals /opt/ml-referrals
bash /opt/ml-referrals/deploy/setup.sh
```

That installs Python + Chromium, builds the venv, and enables the three timers.
The timers are now running but posting can't do much yet — it has no secrets and
no sessions.

**2. From your laptop**, in the repo root:

```bash
bash deploy/push.sh root@your-droplet
```

This rsyncs the two things git can't carry:

- **`.env`** — your secrets (Supabase, affiliate tag/tool, Twitter cookies,
  Groq, Slack). Same file you use locally.
- **`state/`** — the logged-in Mercado Libre session snapshots
  (`ml-session-*.json`), the Twitter cookies file, and the SQLite db if you're
  not on Supabase. The rate-gate scratch dir is excluded.

> The sessions were created on your laptop's IP and are first used from the
> droplet's IP. Mercado Libre may notice that jump. If a session comes up dead
> after the move, re-run `./run login` **on your laptop**, push again, and if it
> keeps happening use `./run login --persist` (a live profile, more robust) —
> `push.sh` carries `state/profiles/` too.

**3. Verify:**

```bash
ssh root@your-droplet \
  "cd /opt/ml-referrals && sudo -u mlref .venv/bin/python run.py session-check"
```

Both sessions should report **logged in**. Then watch the first real post land
on its next scheduled slot, or force one:

```bash
ssh root@your-droplet \
  "cd /opt/ml-referrals && sudo -u mlref .venv/bin/python run.py post --dry-run"
```

## Updating

Code goes through git; `push.sh` pulls it and re-syncs secrets/sessions:

```bash
git push                          # from your laptop
bash deploy/push.sh root@your-droplet
```

If `requirements.txt` changed, re-run `setup.sh` on the droplet (it's
idempotent).

## Operating it

```bash
# what's scheduled and when it next fires
systemctl list-timers 'ml-referrals-*'

# run one now, watch the output
systemctl start ml-referrals-post
journalctl -u ml-referrals-post -f

# recent ingest / post history
journalctl -u ml-referrals-ingest --since today
```

`session-check` Slacks you when a session dies — that's the signal to re-login
on your laptop and `push.sh` again. Until then, a dead session only degrades
(untagged links, product-photo images); it never drops a tweet.

## Prerequisites checklist

- [ ] Supabase schema applied (`supabase_schema.sql`) and a public `offer-cards`
      bucket, **or** `config.json` `store` set to `sqlite`
- [ ] `.env` filled in locally (`./run set-affiliate`, Twitter cookies, etc.)
- [ ] `./run login --role scraping` and `--role affiliate` done on your laptop
- [ ] `./run session-check` green locally before the first `push.sh`
