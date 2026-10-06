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

# Meet++ pre-check: recreating the meetpp-agent interrupts its transcription.
# When an operator token is available, ask the API whether a session is live and
# prompt before continuing.
if [[ -n "${MEETPP_OPERATOR_TOKEN:-}" ]]; then
  echo "==> Checking for active Meet++ sessions…"
  # Capture the HTTP status separately: an auth/connection failure must NOT be
  # mistaken for "zero active sessions". The sessions the agent serves are the
  # running, paused and finalising ones (the endpoint also lists setup ones).
  count_py='import json, sys; print(sum(s.get("status") in ("running", "paused", "finalising") for s in json.load(sys.stdin).get("active_sessions") or []))'
  probe=$(ssh "$HOST" "code=\$(curl -s -o /tmp/meetpp-status.json -w '%{http_code}' -H 'Authorization: Bearer ${MEETPP_OPERATOR_TOKEN}' http://localhost:8080/api/v1/admin/meetpp/status); echo \"\$code\"; if [ \"\$code\" = 200 ]; then python3 -c $(printf '%q' "$count_py") < /tmp/meetpp-status.json || echo '?'; fi; rm -f /tmp/meetpp-status.json" 2>/dev/null || true)
  code=$(printf '%s' "$probe" | sed -n '1p')
  active=$(printf '%s' "$probe" | sed -n '2p')
  if [[ "$code" != "200" ]]; then
    echo "WARNING: Meet++ status endpoint not reachable/authorised (http ${code:-?}). Cannot verify whether a session is live. Continue? [yN]"
    read -r ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
  elif ! [[ "$active" =~ ^[0-9]+$ ]]; then
    echo "WARNING: could not read the Meet++ status response. Cannot verify whether a session is live. Continue? [yN]"
    read -r ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
  elif (( active > 0 )); then
    echo "WARNING: ${active} Meet++ session(s) active. Recreating the agent interrupts transcription. Continue? [yN]"
    read -r ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
  fi
fi

echo "==> Rsyncing $LOCAL_DIR → $HOST:/tmp/meet-stage/"
rsync -avz --delete \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude '*.db' \
  --exclude '*.log' \
  --exclude '.kilo' \
  --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  --exclude 'docs/*.docx' \
  --exclude 'docs/~$*' \
  --exclude 'meetpp-speech' \
  --exclude 'meetpp-agent/tests' \
  "$LOCAL_DIR/" "$HOST:/tmp/meet-stage/"

echo "==> Installing to /opt/meet (preserving .env)…"
ssh "$HOST" '
  set -e
  mkdir -p /opt/meet /var/lib/meet/recordings /var/log/meet /var/lib/meet/meetpp/tts
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

echo "==> Reloading Caddy config (internal-path block + body limit, no downtime)…"
ssh "$HOST" 'cd /opt/meet && docker compose -p meet exec -T caddy caddy reload --config /etc/caddy/Caddyfile' || \
  echo "WARNING: caddy reload failed — check the Caddyfile manually."

echo "==> Verifying coturn is still running…"
ssh "$HOST" '
  systemctl is-active coturn 2>/dev/null && echo "coturn: systemd active" \
    || (docker ps --filter name=coturn --filter status=running --format "coturn docker: {{.Names}} ({{.Status}})")
'

echo "==> Health check…"
ssh "$HOST" 'curl -fsS http://localhost:8080/api/health && echo ""'

echo "==> Done. Visit https://meet.witysk.org"
