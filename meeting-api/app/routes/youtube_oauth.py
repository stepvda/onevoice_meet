"""
YouTube OAuth endpoints for live-streaming automation.

Three legs:
  1. GET  /api/v1/meetings/{id}/youtube/oauth/start
        Owner-only. Mints a signed `state` JWT binding the consent to
        this meeting + user, then redirects to Google's consent screen.
  2. GET  /api/v1/youtube/oauth/callback
        Unauthenticated (Google calls this) — `state` carries the
        meeting binding and the meeting-api JWT secret signs it, so we
        verify the binding before exchanging the code. On success the
        refresh_token + channel handle land on the meeting row.
  3. POST /api/v1/meetings/{id}/youtube/oauth/disconnect
        Owner-only. Revokes the refresh_token with Google and clears
        all related columns. Doesn't touch a running broadcast — the
        host should stop the stream first.

State JWT shape:
  { "iss": "meet-yt-oauth", "sub": <user.sub>, "mid": <meeting.id>,
    "exp": now+10min, "type": "yt_oauth_state" }
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.models import Meeting
from app.services import youtube_live

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600  # 10 minutes — plenty for a consent screen


def _require_owner_meeting(meeting_id: str, user_sub: str, db: Session) -> Meeting:
    """Mirror of routes/streams.py — owner or co-host can configure the
    livestream destinations."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    from app.routes.meetings import is_moderator
    if not is_moderator(m, user_sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    return m


def _mint_state(*, user_sub: str, meeting_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": "meet-yt-oauth",
            "sub": user_sub,
            "mid": meeting_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_STATE_TTL_SECONDS)).timestamp()),
            "type": "yt_oauth_state",
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _verify_state(state: str) -> tuple[str, str]:
    """Returns (user_sub, meeting_id). Raises 400 on any invalid state."""
    try:
        claims = jwt.decode(
            state,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise HTTPException(status_code=400, detail=f"invalid state: {e}")
    if claims.get("type") != "yt_oauth_state" or claims.get("iss") != "meet-yt-oauth":
        raise HTTPException(status_code=400, detail="state wrong type")
    sub = claims.get("sub")
    mid = claims.get("mid")
    if not sub or not mid:
        raise HTTPException(status_code=400, detail="state missing claims")
    return str(sub), str(mid)


@router.post("/meetings/{meeting_id}/youtube/oauth/start")
async def youtube_oauth_start(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Mints a signed `state` JWT and returns the Google consent URL as
    JSON. The SPA opens this URL in a popup so the callback can
    `postMessage` back to the opener tab. A redirect endpoint would
    block popup auth because browsers can't carry Bearer headers on a
    navigation."""
    m = _require_owner_meeting(meeting_id, user.sub, db)
    if not settings.youtube_client_id or not settings.youtube_oauth_redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="YouTube OAuth not configured (YOUTUBE_CLIENT_ID / YOUTUBE_OAUTH_REDIRECT_URI)",
        )
    state = _mint_state(user_sub=user.sub, meeting_id=m.id)
    try:
        url = youtube_live.build_authorize_url(state=state)
    except youtube_live.YouTubeLiveError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"authorize_url": url}


def _callback_html(*, ok: bool, message: str) -> HTMLResponse:
    """Return a tiny self-closing page. The SPA pops up the consent flow
    in a new window and listens for `postMessage` so the modal can update
    "Not connected" → "Connected as …" without a full reload.

    HTML-escape `message` since it can contain Google error strings, and
    JSON-encode the payload so it round-trips into a valid JS object
    literal."""
    import html as _html
    import json as _json

    msg = _html.escape(message)
    title = "YouTube connected" if ok else "YouTube connection failed"
    payload_json = _json.dumps({"ok": ok, "message": message})
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>YouTube connection</title></head>
<body style="font-family: system-ui, sans-serif; background:#0f172a; color:#e2e8f0; padding:24px;">
<h3>{title}</h3>
<p>{msg}</p>
<p style="color:#94a3b8;font-size:13px;">This window will close automatically.</p>
<script>
  try {{
    if (window.opener) {{
      window.opener.postMessage({{ source: "meet-yt-oauth", payload: {payload_json} }}, "*");
    }}
  }} catch (e) {{}}
  setTimeout(function() {{ try {{ window.close(); }} catch (e) {{}} }}, 1500);
</script>
</body></html>"""
    return HTMLResponse(content=body, status_code=200)


@router.get("/youtube/oauth/callback")
async def youtube_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Google's redirect target. Unauthenticated — the `state` JWT proves
    which meeting+user requested the consent."""
    if error:
        return _callback_html(ok=False, message=f"Google: {error}")
    if not code or not state:
        return _callback_html(ok=False, message="missing code or state")

    try:
        user_sub, meeting_id = _verify_state(state)
    except HTTPException as e:
        return _callback_html(ok=False, message=str(e.detail))

    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m:
        return _callback_html(ok=False, message="meeting not found")
    from app.routes.meetings import is_moderator
    if not is_moderator(m, user_sub):
        return _callback_html(ok=False, message="not authorised for this meeting")

    try:
        refresh_token, _access = await youtube_live.exchange_code_for_refresh_token(code=code)
        channel_id, channel_title = await youtube_live.fetch_channel(refresh_token)
    except youtube_live.YouTubeLiveError as e:
        log.exception("youtube oauth callback failed")
        return _callback_html(ok=False, message=str(e))

    m.livestream_youtube_refresh_token = refresh_token
    m.livestream_youtube_channel_id = channel_id
    m.livestream_youtube_channel_title = channel_title
    # Switching to API mode the first time a user connects is the natural
    # expectation — they just clicked "Connect" inside the API-mode column.
    m.livestream_youtube_mode = "api"
    # Light cleanup: a freshly connected account should not reuse a
    # stream id from a previous (revoked) account.
    m.livestream_youtube_stream_id = None
    m.livestream_youtube_api_ingest_url = None
    m.livestream_youtube_api_ingest_key = None
    m.livestream_youtube_broadcast_id = None
    m.livestream_youtube_broadcast_started_at = None
    m.livestream_youtube_watch_url = None
    db.commit()

    return _callback_html(ok=True, message=f"Connected as {channel_title}")


@router.post("/meetings/{meeting_id}/youtube/oauth/disconnect")
async def youtube_oauth_disconnect(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_owner_meeting(meeting_id, user.sub, db)
    if not m.livestream_youtube_refresh_token:
        return {"ok": True, "already_disconnected": True}
    # Try to politely revoke at Google; failure doesn't block our local
    # disconnect since the column erasure is what matters for our access.
    await youtube_live.revoke(m.livestream_youtube_refresh_token)
    m.livestream_youtube_refresh_token = None
    m.livestream_youtube_channel_id = None
    m.livestream_youtube_channel_title = None
    m.livestream_youtube_stream_id = None
    m.livestream_youtube_api_ingest_url = None
    m.livestream_youtube_api_ingest_key = None
    m.livestream_youtube_broadcast_id = None
    m.livestream_youtube_broadcast_started_at = None
    m.livestream_youtube_watch_url = None
    # Bounce back to manual RTMP mode so the host isn't left with a
    # half-configured API-mode meeting.
    m.livestream_youtube_mode = "rtmp"
    db.commit()
    return {"ok": True}


@router.get("/meetings/{meeting_id}/youtube/oauth/status")
async def youtube_oauth_status(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Lightweight read used by the modal to render connect/connected
    state without exposing the refresh token."""
    m = _require_owner_meeting(meeting_id, user.sub, db)
    return {
        "connected": bool(m.livestream_youtube_refresh_token),
        "channel_title": m.livestream_youtube_channel_title,
        "channel_id": m.livestream_youtube_channel_id,
        "mode": m.livestream_youtube_mode,
        "watch_url": m.livestream_youtube_watch_url,
        "broadcast_id": m.livestream_youtube_broadcast_id,
    }
