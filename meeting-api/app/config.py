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
