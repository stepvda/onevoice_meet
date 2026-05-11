#!/usr/bin/env bash
# Deploy meet.witysk.org to turn.witysk.org over SSH.
#
# Usage:  ./scripts/deploy.sh [host]
#   host defaults to root@turn.witysk.org; override for staging, e.g. user@1.2.3.4
#
# Safety: this script NEVER touches coturn. It only syncs /opt/meet and
# restarts the `meet` compose project. If anything looks off, it aborts
# before calling `docker compose up`.

set -euo pipefail

HOST="${1:-root@turn.witysk.org}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Checking coturn status on $HOST before deploy…"
if ! ssh "$HOST" "systemctl is-active coturn >/dev/null 2>&1 || docker ps --filter name=coturn --filter status=running --quiet | grep -q ."; then
  echo "WARNING: coturn is not detected as running on $HOST. Continue? [yN]"
  read -r ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
fi

echo "==> Rsyncing $LOCAL_DIR → $HOST:/tmp/meet-stage/"
rsync -avz --delete \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude '*.db' \
  --exclude '*.log' \
  "$LOCAL_DIR/" "$HOST:/tmp/meet-stage/"

echo "==> Installing to /opt/meet (preserving .env)…"
ssh "$HOST" '
  set -e
  mkdir -p /opt/meet /var/lib/meet/recordings /var/log/meet
  rsync -a --exclude ".env" /tmp/meet-stage/ /opt/meet/
  rm -rf /tmp/meet-stage
'

echo "==> Bringing up compose project 'meet'…"
ssh "$HOST" '
  set -e
  cd /opt/meet
  if [ ! -f .env ]; then
    echo "ERROR: /opt/meet/.env is missing. Run the first-time bootstrap in DEPLOYMENT.md §2."
    exit 2
  fi
  docker compose -p meet up -d --build
  sleep 3
  docker compose -p meet ps
'

echo "==> Verifying coturn is still running…"
ssh "$HOST" '
  systemctl is-active coturn 2>/dev/null && echo "coturn: systemd active" \
    || (docker ps --filter name=coturn --filter status=running --format "coturn docker: {{.Names}} ({{.Status}})")
'

echo "==> Health check…"
ssh "$HOST" 'curl -fsS http://localhost:8080/api/health && echo ""'

echo "==> Done. Visit https://meet.witysk.org"
