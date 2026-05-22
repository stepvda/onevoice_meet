# livekit

LiveKit Server, Egress, and Ingress configuration.

[LiveKit](https://livekit.io) is the WebRTC SFU at the heart of this stack — it's what mixes audio and video for every participant. The companion `egress` worker records or livestreams a room composite; the `ingress` worker pulls MP4s and republishes them as participant tracks (for the in-meeting video-playback feature).

## Files

| File | Purpose |
| --- | --- |
| `livekit.yaml.tpl` | Template for LiveKit Server config. The `__API_KEY__`, `__API_SECRET__`, and `__WEBHOOK_KEY__` placeholders are substituted by the container entrypoint at boot (see [`docker-compose.yml`](../docker-compose.yml)). |

Egress and ingress configs are inlined directly in `docker-compose.yml` via the `EGRESS_CONFIG_BODY` / `INGRESS_CONFIG_BODY` environment variables, because (a) those services accept a config-as-string env var and (b) Compose's `${LIVEKIT_*}` interpolation happens at parse time, which we want for those configs.

## Key config decisions

### Host networking for LiveKit Server

LiveKit needs UDP ports 50000–60000 for WebRTC media. Publishing that range with `-p 50000-60000:50000-60000/udp` is supported by Docker on paper but slow to start in practice. `network_mode: host` is the pragmatic workaround. Signaling (`/rtc/*`) still flows through Caddy on 443, so clients only need a single TLS connection.

Side effect: other containers (bridge network) reach LiveKit via `host.docker.internal:7880`, declared in `extra_hosts:` on each side.

### Disabled built-in TURN

LiveKit can run its own TURN. The reference deployment **doesn't** — it reuses an existing `coturn` install on the host (shared with another service). Setting `turn.enabled: false` in `livekit.yaml.tpl` keeps things simple. `meeting-api` mints short-lived REST-API TURN credentials per RFC 5766 using `TURN_STATIC_AUTH_SECRET` and hands them to the client.

If you don't have an existing coturn, either:

1. Enable LiveKit's built-in TURN by editing `livekit.yaml.tpl` (see [LiveKit docs](https://docs.livekit.io/realtime/self-hosting/deployment/#turn-server)).
2. Run coturn yourself on the host and share the secret.

Built-in TURN is fine for small deployments. A separate coturn helps if you want geographic redundancy or have other services using the same TURN server.

### `empty_timeout` and `departure_timeout` set to ~68 years

LiveKit's default is to close empty rooms after a short timeout (30–90 minutes). The reference deployment wants rooms to stay open until the owner clicks "End meeting" — so both timeouts are set to `2147483647` (max int32 seconds, ~68 years).

If you'd rather have rooms close automatically, set these back to sane defaults (e.g. 1800 = 30 minutes).

### Egress CPU costs

LiveKit Egress refuses to claim a job when its CPU cost exceeds available CPU. The defaults are tuned for a 4+ vCPU box; on the reference 2-vCPU host, defaults make egress reject every job. The `cpu_cost` block in `docker-compose.yml` lowers all costs to 1.0 (room_composite, web, participant) or 0.5 (track_composite, track) so a single recording always claims successfully. Quality may degrade with concurrent recordings on a small box — bump CPU and revert to defaults for production-grade recording.

### Egress = record OR stream, never both as separate jobs

The reference 2-vCPU box can only run ONE egress slot at a time. To support "record + livestream simultaneously," [`egress_mgr.py`](../meeting-api/app/services/egress_mgr.py) builds a single `RoomCompositeEgress` request that combines file output + stream outputs in one job. Toggling either off restarts the egress with the same layout but the remaining outputs. On bigger hosts you could run two separate egress jobs, but the single-slot approach is simpler.

### Ingress URL_INPUT only

The reference deployment uses LiveKit Ingress **only** for `URL_INPUT` (pulling MP4s from meeting-api for the video-playback feature). The RTMP and WHIP ports are listening (the binary won't start otherwise) but never published outside the Docker network. If you'd like to accept RTMP / WHIP ingestion externally, add port publishes to `docker-compose.yml` and adjust the firewall.

## Webhooks

LiveKit POSTs signed-JWT webhook events to `http://127.0.0.1:8080/api/v1/webhooks/livekit`. Events handled:

- `participant_joined` / `participant_left` — Café presence; meeting participant tracking.
- `room_finished` — Cleanup.
- `egress_started` / `egress_updated` / `egress_ended` — Recording + livestream state, including per-destination publish status.
- `ingress_ended` — Playback playlist advance.

The webhook URL is loopback-only — see [`docker-compose.yml`](../docker-compose.yml).
