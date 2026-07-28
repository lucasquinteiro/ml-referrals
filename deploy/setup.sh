#!/usr/bin/env bash
#
# One-time provisioning for a fresh Ubuntu droplet. Run it ON the droplet as
# root (or with sudo). Idempotent — safe to re-run after a code update.
#
#   ssh root@your-droplet
#   git clone https://github.com/lucasquinteiro/ml-referrals /opt/ml-referrals
#   bash /opt/ml-referrals/deploy/setup.sh
#
# It does NOT bring credentials or sessions — those you push from your laptop
# with deploy/push.sh (see deploy/README.md). Without them the timers run but
# posting falls back to untagged links and anonymous /ofertas.
set -euo pipefail

APP_DIR=/opt/ml-referrals
APP_USER=mlref

echo "==> system packages"
apt-get update -qq
# Python + the shared libraries headless Chromium needs.
apt-get install -y -qq python3 python3-venv python3-pip git rsync

echo "==> service user ${APP_USER}"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> virtualenv + dependencies"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Chromium (+ system deps) for Playwright"
# --with-deps installs the OS libraries; must run as root for apt.
"$APP_DIR/.venv/bin/python" -m playwright install-deps chromium
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m playwright install chromium

echo "==> systemd units"
cp "$APP_DIR"/deploy/systemd/*.service "$APP_DIR"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now \
  ml-referrals-ingest.timer \
  ml-referrals-post.timer \
  ml-referrals-session-check.timer

echo
echo "Done. Timers enabled:"
systemctl list-timers 'ml-referrals-*' --no-pager || true
echo
echo "Next, from your laptop:  bash deploy/push.sh root@your-droplet"
echo "  — pushes .env and the state/ sessions this box needs to actually post."
