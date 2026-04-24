# Deploying `meet.witysk.org` to the Hetzner host

Target host: **`turn.witysk.org`** (same box as existing `coturn`).
SSH user: **`root`**.

> ## ⚠️ CRITICAL — Do not disturb coturn
>
> `turn.witysk.org` is already running the `coturn` TURN/STUN server that serves
> `one.witysk.org`'s WebRTC calls. **This deployment must not stop, reconfigure,
> or remove coturn.** If coturn goes down, one.witysk.org calls break.
>
> Safety rules enforced by this deployment:
>
> 1. `meet` runs under a **separate** Docker Compose project (`-p meet`) in
>    `/opt/meet/`. It touches **only** its own containers, volumes, and network.
>    Never run `docker compose down` from any other directory — always `cd /opt/meet` first.
> 2. All LiveKit UDP media ports (50000–60000) are **above** coturn's default
>    relay range (49152–49999). There is no overlap. Before opening firewall
>    rules, verify with `grep -E 'min-port|max-port' /etc/turnserver.conf`.
>    If coturn was reconfigured to a wider range, STOP and talk to the operator
>    before proceeding.
> 3. Port 3478/tcp+udp and 5349/tcp+udp stay bound to coturn. `meet` does not
>    request them.
> 4. `meet`'s Caddy instance binds 80/443. If coturn's host already has a
>    different reverse proxy on 80/443, see §5 "Caddy collision" below before
>    running `docker compose up`.
> 5. Firewall changes in §4 are **additive** (`ufw allow ...`). Never run
>    `ufw reset` or `ufw reload` with a fresh ruleset.
>
> If `systemctl status coturn` shows the service is managed by systemd on the
> host (i.e. coturn is *not* in Docker), `meet`'s containers cannot collide
> with it because they run in isolated network namespaces (except the LiveKit
> container, which uses `network_mode: host` — but its ports are disjoint).

---

## 0. Prerequisites — install Docker

Skip this section if `docker --version` and `docker compose version` already
work on the server. Otherwise install Docker Engine and the Compose v2 plugin
from Docker's official apt repository. **Do not** install the distro's
`docker.io` package — it ships the old Compose v1 script (`docker-compose`),
which this project does not use.

```bash
ssh root@turn.witysk.org '
set -e

# Identify the OS so we use the right apt repo.
. /etc/os-release
echo "OS: $ID $VERSION_CODENAME"

# 1. Remove any old / distro-packaged Docker (does NOT remove images or containers
#    under /var/lib/docker; coturn is not in Docker unless you put it there).
apt-get -y remove docker docker-engine docker.io containerd runc 2>/dev/null || true

# 2. Pre-reqs and Docker GPG key.
apt-get update
apt-get -y install ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# 3. Add Dockers apt repo.
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update

# 4. Install Docker Engine + buildx + Compose plugin.
apt-get -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 5. Enable + start.
systemctl enable --now docker

# 6. Verify.
docker --version
docker compose version
docker run --rm hello-world | tail -5

# 7. Confirm coturn was not disturbed.
systemctl is-active coturn 2>/dev/null || echo "(coturn not a systemd unit — check docker ps)"
'
```

If the distro is Rocky/Alma/RHEL rather than Ubuntu/Debian, swap steps 2–4 for
the `dnf` flow at <https://docs.docker.com/engine/install/rhel/>; everything
else is identical.

## 1. Pre-flight check

Verify the host is ready and coturn is healthy **before** touching anything:

```bash
ssh root@turn.witysk.org '
  set -e
  echo "=== coturn status ==="
  systemctl is-active coturn || echo "(not a systemd service — check docker ps)"
  docker ps --filter name=coturn --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" || true

  echo "=== coturn port config ==="
  if [ -f /etc/turnserver.conf ]; then
    grep -E "^(min-port|max-port|listening-port|tls-listening-port)" /etc/turnserver.conf || true
  fi

  echo "=== what is already on 80/443 ==="
  ss -tlnp "( sport = :80 or sport = :443 )" || true

  echo "=== docker engine ==="
  docker --version
  docker compose version
'
```

Expected:

- `coturn` is active and **its min-port/max-port are within 49152–49999** (or
  whatever doesn't overlap with 50000–60000). If min-port is missing, coturn's
  default is **49152**; default max-port is **65535** — which **does** overlap.
  **If that's the case, see §1.1 before going further.**
- Docker + Compose plugin are installed (§0).

### 1.1 Coturn default port range collision

Coturn's default relay port range is 49152–65535, which overlaps LiveKit's
50000–60000. If the server has no `min-port`/`max-port` set, **cap coturn at
49999 first**:

```bash
# Only if needed; this restarts coturn — confirm no active calls first.
ssh root@turn.witysk.org '
  sed -i.bak "/^#*min-port/d;/^#*max-port/d" /etc/turnserver.conf
  echo "min-port=49152" >> /etc/turnserver.conf
  echo "max-port=49999" >> /etc/turnserver.conf
  systemctl restart coturn
  sleep 2
  systemctl is-active coturn
'
```

Confirm coturn is still serving an existing one.witysk.org call **before** and
**after** this change (ask an active user to place a test call, or run
`turnutils_uclient -v turn.witysk.org` from a client machine).

## 2. First-time bootstrap

Run these from your **laptop**:

```bash
# 1. Ship the repo to the server.
rsync -avz --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude '.env' --exclude '*.db' \
  /Users/nstephane/Dev/onevoice_meet/ \
  root@turn.witysk.org:/tmp/meet-stage/

# 2. Install to /opt/meet and set up data directories.
ssh root@turn.witysk.org '
  set -e
  mkdir -p /opt/meet /var/lib/meet/recordings
  rsync -a --delete /tmp/meet-stage/ /opt/meet/
  rm -rf /tmp/meet-stage
'

# 3. Build the .env file on the server. NEVER scp a .env from your laptop —
#    secrets should live only on the server. Generate LiveKit keys in place.
ssh -t root@turn.witysk.org '
  set -e
  cd /opt/meet
  if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Generating LiveKit API keys in-place…"
    docker run --rm livekit/livekit-server generate-keys > /tmp/lk-keys.txt
    LK_KEY=$(grep -E "^API" /tmp/lk-keys.txt | head -1 | awk "{print \$NF}")
    LK_SECRET=$(grep -E "^secret" /tmp/lk-keys.txt | head -1 | awk "{print \$NF}")
    rm /tmp/lk-keys.txt
    sed -i "s|^LIVEKIT_API_KEY=.*|LIVEKIT_API_KEY=$LK_KEY|" .env
    sed -i "s|^LIVEKIT_API_SECRET=.*|LIVEKIT_API_SECRET=$LK_SECRET|" .env
    sed -i "s|^LIVEKIT_WEBHOOK_KEY=.*|LIVEKIT_WEBHOOK_KEY=$LK_KEY|" .env
    PUBIP=$(curl -4 -s https://ifconfig.co)
    sed -i "s|^LIVEKIT_EXTERNAL_IP=.*|LIVEKIT_EXTERNAL_IP=$PUBIP|" .env
    echo ""
    echo "Now open /opt/meet/.env and set:"
    echo "  JWT_SECRET_KEY         — must match one.witysk.org SECRET_KEY"
    echo "  TURN_STATIC_AUTH_SECRET — must match coturn static-auth-secret"
    echo ""
    echo "Find the coturn secret with:"
    echo "  grep static-auth-secret /etc/turnserver.conf"
  else
    echo ".env already exists — not regenerated."
  fi
'

# 4. Edit .env on the server to plug in the two shared secrets.
ssh -t root@turn.witysk.org '${EDITOR:-nano} /opt/meet/.env'
```

## 3. Bring up the stack

```bash
ssh root@turn.witysk.org '
  set -e
  cd /opt/meet

  # Dry-run: shows which containers/ports/volumes would be created.
  docker compose -p meet config | head -80

  # Build the frontend and start everything.
  docker compose -p meet up -d --build
  sleep 5
  docker compose -p meet ps
'
```

Verify:

```bash
ssh root@turn.witysk.org '
  echo "=== meet containers ==="
  cd /opt/meet && docker compose -p meet ps

  echo "=== coturn still up? ==="
  systemctl is-active coturn 2>/dev/null || docker ps --filter name=coturn --format "{{.Names}}\t{{.Status}}"

  echo "=== health ==="
  curl -fsS http://localhost:8080/api/health && echo ""
  curl -fsS -o /dev/null -w "livekit 7880: %{http_code}\n" http://localhost:7880/
'
```

Expected:

- All `meet-*` containers say `Up`.
- `coturn` status is still `active`.
- `/api/health` returns `{"status":"ok"}`.

## 4. Firewall (UFW — additive only)

```bash
ssh root@turn.witysk.org '
  # What is already open?
  ufw status numbered

  # Add ONLY the new rules needed by meet. Do not touch coturn rules.
  ufw allow 7881/tcp comment "livekit ICE TCP"
  ufw allow 50000:60000/udp comment "livekit WebRTC media"

  # 80/443 for Caddy — if not already allowed for something else.
  ufw allow 80/tcp comment "caddy http" || true
  ufw allow 443/tcp comment "caddy https" || true

  ufw status numbered
'
```

**Do not** run `ufw reset` or load a fresh ruleset — it would drop coturn's
rules (3478, 5349, 49152–49999/udp).

## 5. Caddy collision (if another proxy exists on :80/:443)

If the host **already** has something on 80/443 (nginx, an existing Caddy for
turn.witysk.org's admin UI, etc.), do one of:

**Option A — add a vhost to the existing proxy**, and bind `meet`'s Caddy to
loopback-only by editing `/opt/meet/docker-compose.yml`:

```yaml
  caddy:
    ports:
      - "127.0.0.1:8081:80"  # change from "80:80"
      - "127.0.0.1:8443:443" # change from "443:443"
```

Then in the existing proxy (example nginx):

```nginx
server {
  listen 443 ssl http2;
  server_name meet.witysk.org;
  # existing TLS cert config
  location / { proxy_pass http://127.0.0.1:8443; proxy_set_header Host $host; }
}
```

**Option B — merge `meet` into the existing Caddy.** Copy
`/opt/meet/caddy/Caddyfile`'s `meet.witysk.org { ... }` block into the
existing Caddyfile, remove the `caddy` service from `docker-compose.yml`, and
reload the existing Caddy.

Either option leaves coturn untouched.

## 6. Configuring the LiveKit webhook from one.witysk.org

Nothing to do on one.witysk.org. The webhook runs internally: LiveKit → `meeting-api`.

## 7. Ongoing operations

Update to a new version:

```bash
rsync -avz --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' --exclude '.env' --exclude '*.db' \
  /Users/nstephane/Dev/onevoice_meet/ \
  root@turn.witysk.org:/tmp/meet-stage/

ssh root@turn.witysk.org '
  set -e
  rsync -a --exclude ".env" /tmp/meet-stage/ /opt/meet/
  cd /opt/meet
  docker compose -p meet up -d --build
  docker compose -p meet ps
  systemctl is-active coturn 2>/dev/null && echo "coturn OK"
'
```

Logs:

```bash
ssh root@turn.witysk.org 'cd /opt/meet && docker compose -p meet logs --tail=200 meeting-api'
ssh root@turn.witysk.org 'cd /opt/meet && docker compose -p meet logs --tail=200 livekit'
```

Backup the SQLite DB:

```bash
ssh root@turn.witysk.org 'cp /var/lib/meet/meet.db /var/lib/meet/meet.db.$(date +%Y%m%d)'
```

Stop `meet` without touching coturn:

```bash
ssh root@turn.witysk.org 'cd /opt/meet && docker compose -p meet down'
```

Emergency restart of only the LiveKit container (e.g. after config change):

```bash
ssh root@turn.witysk.org 'cd /opt/meet && docker compose -p meet restart livekit'
```

## 8. Rolling back

```bash
ssh root@turn.witysk.org '
  cd /opt/meet
  docker compose -p meet down
  # /var/lib/meet and coturn remain untouched.
'
```

To fully remove `meet`:

```bash
ssh root@turn.witysk.org '
  cd /opt/meet
  docker compose -p meet down -v      # removes meet-only volumes
  rm -rf /opt/meet /var/lib/meet      # wipes app + data
  # coturn, /etc/turnserver.conf, /var/log/coturn, etc. are untouched.
'
```

Coturn firewall rules (3478, 5349, 49152–49999/udp) are left in place — remove
manually only if you are also retiring coturn.

## 9. Smoke test post-deploy

Run through §18 of the spec:

1. Open `https://meet.witysk.org` in two browsers.
2. Sign in on one.witysk.org in browser A (gets `access_token` in localStorage
   on `.witysk.org`). If localStorage isn't shared across subdomains, paste the
   token manually in DevTools for now — Phase 12 adds a proper cookie bridge.
3. Create a meeting in browser A.
4. Copy the join link, paste into browser B (anonymous), enter name, join.
5. Confirm audio/video flow both ways.
6. Disconnect UDP on browser B's network (e.g. via firewall rule blocking
   50000–60000/udp outbound); reconnect and confirm the call falls back to
   coturn TLS 5349 without interruption.
7. Re-check `systemctl is-active coturn` — still active.

If any of those fail, see logs in §7 or roll back per §8.
