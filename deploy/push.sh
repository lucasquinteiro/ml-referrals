#!/usr/bin/env bash
#
# Push code + credentials + sessions from your laptop to the droplet.
# Run it FROM your laptop, in the repo root:
#
#   bash deploy/push.sh root@your-droplet
#
# Code is normally deployed with `git pull` on the droplet, but the two things
# git can't carry live here: your .env (secrets) and state/ (the logged-in
# Mercado Libre sessions + the SQLite db if you use it). Both are gitignored,
# so this rsyncs them directly.
set -euo pipefail

TARGET="${1:-}"
APP_DIR=/opt/ml-referrals
if [[ -z "$TARGET" ]]; then
  echo "usage: bash deploy/push.sh user@host" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

echo "==> code (git)"
# Prefer git on the droplet; this just makes sure it's current.
ssh "$TARGET" "cd $APP_DIR && git pull --ff-only" || {
  echo "   (git pull failed — is $APP_DIR a clone? see deploy/README.md)"; }

echo "==> .env"
[[ -f .env ]] && rsync -av .env "$TARGET:$APP_DIR/.env" || echo "   no local .env; skipping"

echo "==> sessions + state (state/)"
# The whole state dir: ML session snapshots (or profiles/ if you use --persist),
# the twitter cookies file if present, and the SQLite db if store=sqlite.
# --mkpath needs rsync 3.2.3+; the mkdir fallback covers older droplets.
ssh "$TARGET" "mkdir -p $APP_DIR/state"
rsync -av --delete-after \
  --exclude 'mlgate/' \
  state/ "$TARGET:$APP_DIR/state/"

echo "==> fixing ownership on the droplet"
ssh "$TARGET" "chown -R mlref:mlref $APP_DIR/state $APP_DIR/.env 2>/dev/null || true"

echo
echo "Done. Verify on the droplet:"
echo "  ssh $TARGET 'cd $APP_DIR && sudo -u mlref .venv/bin/python run.py session-check'"
