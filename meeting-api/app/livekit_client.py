"""
LiveKit server-side helpers: token minting and admin API calls.

Tokens are JWTs signed with LIVEKIT_API_SECRET. They carry a VideoGrant that
tells the LiveKit server what this participant is allowed to do inside a room.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha1
import hmac
import base64

from livekit import api

from app.config import settings


def _token() -> api.AccessToken:
    return api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)


def mint_participant_token(
    *,
    room_name: str,
    identity: str,
    display_name: str,
    is_owner: bool,
    metadata: dict | None = None,
    ttl_hours: int = 6,
    allow_screenshare: bool = True,
) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_publish_data=True,
        can_subscribe=True,
        can_update_own_metadata=True,
        room_admin=is_owner,
    )
    # Owners always keep full publish rights. For everyone else, when the
    # meeting forbids participant screenshare, restrict publishable sources
    # to camera + microphone so the LiveKit server refuses any screenshare
    # publish attempt at the SFU layer (not just a hidden UI button).
    if not is_owner and not allow_screenshare:
        grants.can_publish_sources = ["camera", "microphone"]
    tok = (
        _token()
        .with_identity(identity)
        .with_name(display_name)
        .with_grants(grants)
        .with_ttl(timedelta(hours=ttl_hours))
    )
    if metadata:
        import json

        tok = tok.with_metadata(json.dumps(metadata))
    return tok.to_jwt()


def livekit_api() -> "api.LiveKitAPI":
    """
    Returns an async LiveKit server-API client. Intended for phase 7+ when
    moderation endpoints actually call the SFU. Callers must `await client.aclose()`.
    """
    return api.LiveKitAPI(
        url=settings.livekit_server_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


def short_lived_turn_credentials(user_label: str) -> dict | None:
    """
    Generate RFC 5766 REST API-compatible short-lived TURN credentials for
    coturn. Returns None if TURN_STATIC_AUTH_SECRET is not configured.

    Usage on the client:
        iceServers: [
          { urls: ['turn:turn.witysk.org:3478?transport=udp',
                   'turns:turn.witysk.org:5349?transport=tcp'],
            username: <returned username>,
            credential: <returned credential> }
        ]
    """
    if not settings.turn_static_auth_secret:
        return None
    expiry = int((datetime.now(timezone.utc) + timedelta(seconds=settings.turn_ttl_seconds)).timestamp())
    username = f"{expiry}:{user_label}"
    digest = hmac.new(
        settings.turn_static_auth_secret.encode("utf-8"),
        username.encode("utf-8"),
        sha1,
    ).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return {
        "username": username,
        "credential": credential,
        "ttl": settings.turn_ttl_seconds,
        "urls": [
            f"turn:{settings.turn_host}:3478?transport=udp",
            f"turn:{settings.turn_host}:3478?transport=tcp",
            f"turns:{settings.turn_host}:5349?transport=tcp",
        ],
    }
