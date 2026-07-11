from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LiveKit
    livekit_api_key: str
    livekit_api_secret: str
    livekit_ws_url: str = "wss://meet.witysk.org/rtc"
    livekit_webhook_key: str
    # Internal URL used by meeting-api to call LiveKit's server API.
    # With network_mode: host, LiveKit listens on the host at 7880.
    livekit_server_url: str = "http://host.docker.internal:7880"

    # PiP compositor service (a separate Docker container running
    # Puppeteer + Chromium). meeting-api POSTs to it whenever a meeting
    # toggles `pip_enabled`. Reachable via Docker DNS within the
    # internal bridge network.
    compositor_url: str = "http://compositor:8090"

    # meeting-api
    meeting_api_port: int = 8080
    database_url: str = "sqlite:////var/lib/meet/meet.db"
    recordings_dir: str = "/var/lib/meet/recordings"
    # Playlist MP4s for in-meeting playback, laid out as
    # <playback_dir>/<meeting_id>/<item_id>.mp4. Previously derived from
    # `recordings_dir`'s parent, which silently pinned it to /var/lib/meet.
    # Explicit now so the (large) playlist library can sit on its own volume —
    # production sets PLAYBACK_DIR=/mnt/video1/meetvideos in `.env`. Must be
    # mounted into the meeting-api container at the same absolute path.
    playback_dir: str = "/var/lib/meet/playback"
    branding_dir: str = "/var/lib/meet/branding"
    branding_max_bytes: int = 2 * 1024 * 1024  # 2 MB cap for upload
    chat_attachments_dir: str = "/var/lib/meet/chat-attachments"
    chat_attachment_max_bytes: int = 5 * 1024 * 1024  # 5 MB cap per image
    facepics_dir: str = "/var/lib/meet/facepics"
    facepic_max_bytes: int = 5 * 1024 * 1024  # 5 MB
    # HMAC key for voucher codes. Set to a long random string in .env. Vouchers
    # issued before this key was set become un-redeemable, which is the
    # intended behaviour if the key is rotated.
    voucher_signing_key: str = ""
    # one.witysk.org user_ids that can issue / list vouchers. Hard-coded by spec.
    voucher_admin_user_ids: list[str] = ["1", "404"]

    # Platform admins — emails that get is_platform_admin=True on first sight.
    # Re-applied on every startup, so adding a new email here promotes any
    # already-existing user with that email next time the API restarts. Match
    # is case-insensitive. Set as a comma-separated list in `.env`.
    platform_admin_emails: list[str] = [
        "stephane@stepvda.com",
        "david.iorlano@pm.me",
    ]

    # ─── IDS / IP blocking ──────────────────────────────────────────────
    ids_enabled: bool = True
    # Auth-failure brute force: N failures from the same IP within the window
    # → temp-block that IP for `ids_temp_block_minutes`.
    ids_brute_force_threshold: int = 10
    ids_brute_force_window_seconds: int = 300
    # 2FA brute force is tighter — successful password but failing 2FA codes.
    ids_twofa_brute_force_threshold: int = 5
    ids_twofa_brute_force_window_seconds: int = 300
    # Path scanning — many 404s in a row (probing for endpoints).
    ids_path_scan_threshold: int = 30
    ids_path_scan_window_seconds: int = 60
    ids_temp_block_minutes: int = 30
    # Cap the in-memory event ring (per-IP). Older events evicted FIFO. Keeps
    # memory bounded under flooding without losing the most recent signals.
    ids_max_events_per_ip: int = 200

    # PayPal billing — set these in `.env` from the PayPal Business account's
    # Developer dashboard. Plan and prices are spec'd by the operator:
    #   €2 / month  (recurring subscription) — `paypal_plan_id_monthly`
    #   €20 / year  (one-shot Order)         — handled inline at checkout
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    # Sandbox: api-m.sandbox.paypal.com / Live: api-m.paypal.com
    paypal_api_base: str = "https://api-m.paypal.com"
    paypal_plan_id_monthly: str = ""
    paypal_plan_id_annual: str = ""
    # Webhook ID (from the PayPal dashboard) — used to verify incoming
    # webhook signatures. If empty, signature checks are skipped (insecure;
    # only acceptable in dev / sandbox).
    paypal_webhook_id: str = ""
    # One-shot prices + currency. Monthly = bill-once (2€, 30 days, no
    # auto-renewal — the alternative to subscribing). Annual = 20€, 365 days.
    paypal_monthly_price: str = "2.00"
    paypal_annual_price: str = "20.00"
    paypal_annual_currency: str = "EUR"
    paypal_monthly_currency: str = "EUR"
    recording_retention_days: int = 30
    # When the filesystem holding `recordings_dir` reaches this fraction of
    # capacity, the oldest completed recordings are deleted to make room.
    recording_disk_cap_ratio: float = 0.90
    # Override default 720p30 H.264 Main encoding with 1080p30. Enable only if
    # the egress container has the CPU headroom (≈1 extra core).
    recording_preset_1080p: bool = False

    # JWT — HS256 shared secret, matching one.witysk.org.
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_audience: str = "meet.witysk.org"

    # coturn
    turn_host: str = "turn.witysk.org"
    turn_static_auth_secret: str = ""
    turn_ttl_seconds: int = 3600

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Rate limit
    anon_token_rate_per_hour: int = 30

    # Public URL (for join links in responses)
    public_url: str = "https://meet.witysk.org"

    # --- Live HLS for the TI-TV public channel -------------------------------
    # The mobile apps need a *castable / backgroundable* live stream, which the
    # WebRTC public room can't provide. When the public channel whose slug is
    # `titv_public_slug` is streaming, its RoomCompositeEgress also writes an
    # HLS output (segments + a sliding-window live playlist) under
    # `<recordings_dir>/hls/<slug>/`, served by Caddy at
    # `<public_url>/hls/<slug>/live.m3u8`. Reuses the composite's encoded frames
    # (no second encode) so the extra cost is roughly one more muxer.
    titv_public_slug: str = "titv"
    hls_enabled: bool = True
    hls_segment_seconds: int = 6          # per-segment length (latency vs. request rate)
    hls_retention_seconds: int = 180      # prune .ts segments older than this (disk cap)

    # Resend (email). Source the key from one.witysk.org's RESEND_API_KEY.
    # `from_email` follows onevoice's convention (FROM_EMAIL env var); supports
    # the standard "Display Name <addr@domain>" format.
    resend_api_key: str = ""
    from_email: str = "meet@witysk.org"
    invite_reply_to: str = ""

    # YouTube — manual per-recording publish.
    # OAuth2 desktop client; obtain refresh_token via a one-time flow.
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    youtube_default_privacy: str = "unlisted"  # public | unlisted | private

    # YouTube Live (per-meeting OAuth + Data API automation). Reuses
    # `youtube_client_id` / `youtube_client_secret` above as the OAuth
    # client; each meeting owner connects their own channel through the
    # browser. Redirect URI must match what's registered in Google Cloud
    # Console for the same client. Default works for the production host;
    # override in `.env` for dev (e.g. http://localhost:5173/api/v1/...).
    youtube_oauth_redirect_uri: str = ""
    # Default broadcast settings used when Meet provisions broadcasts on
    # the owner's behalf. Privacy mirrors `youtube_default_privacy` unless
    # overridden here. Title/description templates can include
    # `{meeting_title}` which is substituted at provision time.
    youtube_live_default_title: str = "{meeting_title} — Live"
    youtube_live_default_description: str = ""
    # Default for managed-mode broadcasts. `public` so the broadcast
    # surfaces on the channel's /live page, in YouTube search, and in
    # subscriber feeds — the host-facing intent for "go live". Keep it
    # decoupled from `youtube_default_privacy` (which governs the
    # recording-upload flow and stays "unlisted" by default).
    youtube_live_default_privacy: str = "public"
    # Hard rotation point. YouTube cuts broadcasts at 12h; we rotate
    # earlier so the next broadcast is bound and live before the old
    # one is forced complete. 11h30m is a safe margin.
    youtube_broadcast_rotate_after_seconds: int = 11 * 3600 + 30 * 60

    # whisper.cpp server — self-hosted free transcription of completed
    # recordings. Set to empty to disable the transcript pipeline entirely.
    # The default points to the sidecar container defined in docker-compose.
    whisper_url: str = "http://whisper:8080/inference"


settings = Settings()
