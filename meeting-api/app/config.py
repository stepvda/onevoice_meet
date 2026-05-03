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

    # meeting-api
    meeting_api_port: int = 8080
    database_url: str = "sqlite:////var/lib/meet/meet.db"
    recordings_dir: str = "/var/lib/meet/recordings"
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
    recording_disk_cap_ratio: float = 0.75
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


settings = Settings()
