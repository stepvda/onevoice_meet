"""
YouTube Live streaming automation — OAuth + Data API v3.

Owners connect their YouTube channel from the in-meeting livestream config
panel ("OAuth + API (managed)" mode). Once connected, Meet handles the full
broadcast lifecycle on the server:

  • provisions one persistent `liveStream` resource per meeting (the RTMP
    ingest endpoint; URL + stream key are stored on the meeting row and
    reused forever)
  • creates a `liveBroadcast` each time the user clicks "Start streaming",
    binds it to the persistent stream, transitions testing → live
  • watches stream health every ~30s and restarts LiveKit egress if the
    YouTube side reports a bad/stalled stream
  • rotates broadcasts every ~11h30m (YouTube hard-caps a single broadcast
    at 12h); the next broadcast binds to the same ingest key so the egress
    never has to bounce
  • polls concurrent viewer count and stores it on
    `LivestreamDestinationState(platform_id="youtube").viewer_count`

The single OAuth client (settings.youtube_client_id / _client_secret) is
also used by the recording-upload feature in `services/youtube.py`; this
module adds the per-meeting refresh-token storage and the live-streaming
scopes/endpoints.

Token refresh: access tokens are short-lived and never persisted. Each
call exchanges the stored refresh token for a fresh access token
on demand — same pattern as services/youtube.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

import httpx

from app.config import settings
from app.models import Meeting

log = logging.getLogger(__name__)

# OAuth scopes required for Data API v3 live streaming. `youtube` covers
# read/write of liveBroadcasts + liveStreams; `youtube.readonly` would
# block bind/transition. We do NOT request `youtube.upload` here — the
# recording-upload feature uses its own (single global) refresh token.
OAUTH_SCOPES = "https://www.googleapis.com/auth/youtube"

OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeLiveError(Exception):
    """Anything non-2xx from Google or a logic error in the lifecycle.

    The string form is safe to surface to the host (carries the HTTP
    status + truncated response body, not stack traces or secrets).
    """


@dataclass(frozen=True)
class ProvisionedStream:
    """Result of `ensure_provisioned_stream` — the RTMP ingest endpoint
    LiveKit egress should push to. `stream_id` is the persistent
    `liveStream` resource we'll reuse forever."""
    stream_id: str
    ingest_url: str
    ingest_key: str


@dataclass(frozen=True)
class BroadcastInfo:
    broadcast_id: str
    watch_url: str
    life_cycle_status: str  # created | ready | testing | live | complete | revoked


@dataclass(frozen=True)
class StreamHealth:
    """Subset of `liveStream.status` we care about for the watchdog."""
    stream_status: str   # active | inactive | error | ready | created
    health_status: str   # good | ok | bad | noData
    # Configuration issues reported by YouTube (e.g. "noData" with detail
    # `gotNoData`). Empty when healthy.
    last_issue: str | None


# ─── OAuth ─────────────────────────────────────────────────────────────


def build_authorize_url(*, state: str) -> str:
    """Build the Google consent screen URL. The caller signs `state` so
    the callback can prove it originated here AND identify which meeting
    the consent applies to."""
    if not settings.youtube_client_id:
        raise YouTubeLiveError("YOUTUBE_CLIENT_ID is not configured")
    redirect = settings.youtube_oauth_redirect_uri
    if not redirect:
        raise YouTubeLiveError("YOUTUBE_OAUTH_REDIRECT_URI is not configured")
    from urllib.parse import urlencode
    params = {
        "client_id": settings.youtube_client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "access_type": "offline",       # → returns a refresh_token
        "prompt": "consent",            # force refresh_token even on re-consent
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_refresh_token(*, code: str) -> tuple[str, str]:
    """Exchange the one-time auth code for (refresh_token, access_token)."""
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise YouTubeLiveError("YouTube OAuth client not configured")
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.youtube_oauth_redirect_uri,
            },
        )
    if r.status_code != 200:
        raise YouTubeLiveError(f"code exchange failed: HTTP {r.status_code} {r.text[:300]}")
    body = r.json()
    rt = body.get("refresh_token")
    at = body.get("access_token")
    if not rt or not at:
        raise YouTubeLiveError(f"code exchange returned no refresh_token: {body}")
    return rt, at


async def _access_token(refresh_token: str) -> str:
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise YouTubeLiveError("YouTube OAuth client not configured")
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise YouTubeLiveError(f"refresh failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


async def revoke(refresh_token: str) -> None:
    """Tell Google to forget this refresh token. Failure is logged but not
    raised — the column will be cleared regardless."""
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            await cli.post(OAUTH_REVOKE_URL, data={"token": refresh_token})
    except Exception:
        log.exception("youtube revoke failed")


# ─── Data API helpers ──────────────────────────────────────────────────


async def _api(
    access_token: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: Any | None = None,
) -> dict:
    """Thin httpx wrapper. Raises YouTubeLiveError on non-2xx so callers
    can surface a useful message to the host without leaking JSON internals."""
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.request(method, url, headers=headers, params=params, json=json_body)
    if r.status_code >= 400:
        # 401 typically means the refresh token has been revoked client-side
        # (user yanked permission from myaccount.google.com). Caller may
        # want to clear the stored refresh_token on this signal — surface
        # the status code so it can.
        raise YouTubeLiveError(f"{method} {path} → HTTP {r.status_code} {r.text[:400]}")
    return r.json()


async def fetch_channel(refresh_token: str) -> tuple[str, str]:
    """Returns (channel_id, channel_title). Used right after the OAuth
    callback so the UI can show "Connected as <handle>"."""
    at = await _access_token(refresh_token)
    body = await _api(at, "GET", "/channels", params={"part": "snippet", "mine": "true"})
    items = body.get("items") or []
    if not items:
        raise YouTubeLiveError("no channel found for this Google account")
    snip = items[0].get("snippet") or {}
    title = snip.get("title") or "YouTube channel"
    chid = items[0].get("id") or ""
    return chid, title


# ─── liveStream provisioning ───────────────────────────────────────────


async def ensure_provisioned_stream(m: Meeting) -> ProvisionedStream:
    """Idempotent: return the persistent `liveStream` for this meeting,
    creating one on the first call. Caller persists `stream_id`,
    `ingest_url`, `ingest_key` to the meeting row.

    The ingest URL+key returned here is what LiveKit egress should push
    to. Calling this on every reconcile is safe: if the meeting already
    has `livestream_youtube_stream_id` we just refetch the resource to
    pick up the current ingestion address."""
    if not m.livestream_youtube_refresh_token:
        raise YouTubeLiveError("YouTube account not connected for this meeting")
    at = await _access_token(m.livestream_youtube_refresh_token)

    if m.livestream_youtube_stream_id:
        body = await _api(
            at,
            "GET",
            "/liveStreams",
            params={"part": "cdn,status", "id": m.livestream_youtube_stream_id},
        )
        items = body.get("items") or []
        if items:
            cdn = items[0].get("cdn") or {}
            ingest = cdn.get("ingestionInfo") or {}
            url = ingest.get("ingestionAddress") or ""
            key = ingest.get("streamName") or ""
            if url and key:
                return ProvisionedStream(
                    stream_id=m.livestream_youtube_stream_id, ingest_url=url, ingest_key=key
                )
        # Stream id stale (e.g. deleted in Studio). Fall through to create
        # a new one.

    # Create a fresh persistent liveStream. Quality / format choices match
    # what LiveKit egress emits: H.264, 1080p OR 720p depending on env
    # preset; YouTube auto-detects bitrate.
    resolution = "1080p" if settings.recording_preset_1080p else "720p"
    payload = {
        "snippet": {
            "title": f"Meet — {m.display_title[:80]}",
        },
        "cdn": {
            "frameRate": "30fps",
            "ingestionType": "rtmp",
            "resolution": resolution,
        },
        "contentDetails": {"isReusable": True},
    }
    created = await _api(
        at, "POST", "/liveStreams", params={"part": "snippet,cdn,contentDetails,status"}, json_body=payload
    )
    sid = created.get("id")
    cdn = created.get("cdn") or {}
    ingest = cdn.get("ingestionInfo") or {}
    url = ingest.get("ingestionAddress") or ""
    key = ingest.get("streamName") or ""
    if not (sid and url and key):
        raise YouTubeLiveError(f"liveStream create returned incomplete response: {created}")
    return ProvisionedStream(stream_id=sid, ingest_url=url, ingest_key=key)


async def get_stream_health(m: Meeting) -> StreamHealth:
    if not m.livestream_youtube_refresh_token or not m.livestream_youtube_stream_id:
        raise YouTubeLiveError("stream not provisioned")
    at = await _access_token(m.livestream_youtube_refresh_token)
    body = await _api(
        at, "GET", "/liveStreams",
        params={"part": "status", "id": m.livestream_youtube_stream_id},
    )
    items = body.get("items") or []
    if not items:
        raise YouTubeLiveError("liveStream not found")
    st = items[0].get("status") or {}
    hs = st.get("healthStatus") or {}
    last = hs.get("lastUpdateTimeSeconds")  # noqa: F841
    return StreamHealth(
        stream_status=st.get("streamStatus") or "",
        health_status=hs.get("status") or "",
        last_issue=(hs.get("configurationIssues") or [{}])[0].get("description") if hs.get("configurationIssues") else None,
    )


# ─── liveBroadcast lifecycle ───────────────────────────────────────────


async def get_broadcast_lifecycle(m: Meeting) -> str | None:
    """Return the broadcast's current `lifeCycleStatus`
    (`created` | `ready` | `testing` | `live` | `complete` | `revoked`)
    or None if it can't be fetched.

    Used by the supervisor to decide whether a `transition→live` call is
    even meaningful. Once a broadcast is `live` (typically via
    `enableAutoStart=True` flipping it the moment bytes arrive),
    re-transitioning is a no-op that costs 50 quota units and returns
    HTTP 403 `Invalid transition`. Checking lifecycle first costs 1 unit
    and gates the noisy transition retry."""
    if not m.livestream_youtube_refresh_token or not m.livestream_youtube_broadcast_id:
        return None
    try:
        at = await _access_token(m.livestream_youtube_refresh_token)
        body = await _api(
            at, "GET", "/liveBroadcasts",
            params={"part": "status", "id": m.livestream_youtube_broadcast_id},
        )
    except YouTubeLiveError as e:
        log.warning("broadcast lifecycle fetch failed: %s", str(e)[:200])
        return None
    items = body.get("items") or []
    if not items:
        return None
    return (items[0].get("status") or {}).get("lifeCycleStatus")


def _default_privacy() -> str:
    p = (settings.youtube_live_default_privacy or settings.youtube_default_privacy or "unlisted").lower()
    if p not in ("public", "unlisted", "private"):
        return "unlisted"
    return p


def _broadcast_title(m: Meeting) -> str:
    tmpl = settings.youtube_live_default_title or "{meeting_title} — Live"
    return tmpl.replace("{meeting_title}", m.display_title)[:100]


async def create_broadcast(m: Meeting) -> BroadcastInfo:
    """Create + bind a new liveBroadcast on the meeting's persistent stream
    and transition it to `live`. Returns the broadcast id and public watch
    URL. Caller persists those to the meeting row.

    Note YouTube requires the upstream RTMP ingest to be receiving bytes
    BEFORE you can transition to `live` (otherwise the API returns
    redundantTransition / invalidTransition). When called from the
    supervisor's rotation path the previous broadcast is still serving
    the same ingest key, so transition-to-live succeeds immediately.
    On first-start (no bytes flowing yet) the supervisor retries the
    transition on the next tick once stream_status flips to "active"."""
    if not m.livestream_youtube_refresh_token or not m.livestream_youtube_stream_id:
        raise YouTubeLiveError("connect a YouTube account and provision a stream first")
    at = await _access_token(m.livestream_youtube_refresh_token)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    payload = {
        "snippet": {
            "title": _broadcast_title(m),
            "scheduledStartTime": now,
            "description": (settings.youtube_live_default_description or "")[:5000],
        },
        "status": {
            "privacyStatus": _default_privacy(),
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": True,
            "enableAutoStop": False,
            "monitorStream": {"enableMonitorStream": False},
            "enableDvr": True,
            "recordFromStart": True,
        },
    }
    created = await _api(
        at, "POST", "/liveBroadcasts",
        params={"part": "snippet,status,contentDetails"},
        json_body=payload,
    )
    bid = created.get("id")
    if not bid:
        raise YouTubeLiveError(f"broadcast create returned no id: {created}")

    # Bind the broadcast to the persistent stream.
    await _api(
        at, "POST", "/liveBroadcasts/bind",
        params={
            "part": "id,contentDetails",
            "id": bid,
            "streamId": m.livestream_youtube_stream_id,
        },
    )
    return BroadcastInfo(
        broadcast_id=bid,
        watch_url=f"https://www.youtube.com/watch?v={bid}",
        life_cycle_status=(created.get("status") or {}).get("lifeCycleStatus") or "created",
    )


async def transition_broadcast(m: Meeting, *, to: str) -> str:
    """Transition the meeting's current broadcast. `to` is one of
    'testing' | 'live' | 'complete'. Returns the resulting lifeCycleStatus.

    Idempotent against YouTube's redundantTransition error — if the
    broadcast is already in the requested state we swallow the 403 and
    return the current status."""
    if not m.livestream_youtube_refresh_token or not m.livestream_youtube_broadcast_id:
        raise YouTubeLiveError("no active broadcast")
    at = await _access_token(m.livestream_youtube_refresh_token)
    try:
        body = await _api(
            at, "POST", "/liveBroadcasts/transition",
            params={
                "broadcastStatus": to,
                "id": m.livestream_youtube_broadcast_id,
                "part": "id,status",
            },
        )
    except YouTubeLiveError as e:
        msg = str(e)
        if "redundantTransition" in msg or "invalidTransition" in msg:
            log.info("youtube transition %s already in state: %s", to, msg[:200])
            return to
        raise
    return (body.get("status") or {}).get("lifeCycleStatus") or to


async def complete_broadcast(m: Meeting) -> None:
    try:
        await transition_broadcast(m, to="complete")
    except YouTubeLiveError:
        log.exception("youtube broadcast complete failed; ignoring")


# ─── Viewer count ──────────────────────────────────────────────────────


async def supervise_meeting(m: Meeting, db: Any) -> int:
    """Per-meeting tick of the supervisor. Returns the number of changes
    made (broadcast rotations, broadcast transitions, viewer-count
    updates) so the scheduler can log an informative count.

    Responsibilities:
      1. **Broadcast rotation**: if the current broadcast has been live
         for ≥ `youtube_broadcast_rotate_after_seconds`, create a fresh
         broadcast bound to the SAME persistent stream, transition
         old → complete and new → live. Same RTMP key, so LiveKit egress
         is undisturbed.
      2. **Stale broadcast cleanup**: if the meeting has no active
         egress but still carries a `broadcast_id`, complete it so the
         public page reflects "ended".
      3. **Initial transition to live**: a freshly-created broadcast
         lands in `ready`; YouTube only accepts the `live` transition
         once it has received some RTMP data. We retry every tick until
         it succeeds.
      4. **Viewer count poll**: when a broadcast is live, fetch
         `concurrentViewers` and upsert
         `LivestreamDestinationState(platform_id="youtube").viewer_count`.
    """
    from datetime import datetime, timezone
    from app.config import settings as _settings
    from app.models import LivestreamDestinationState

    changes = 0

    # 2. Stale broadcast: no egress but a broadcast is still flagged.
    # Complete it and clear so the next start path picks up cleanly.
    if not m.livestream_egress_id and m.livestream_youtube_broadcast_id:
        try:
            await complete_broadcast(m)
        except Exception:
            log.exception("supervise: complete stale broadcast failed")
        m.livestream_youtube_broadcast_id = None
        m.livestream_youtube_broadcast_started_at = None
        m.livestream_youtube_watch_url = None
        db.commit()
        return 1

    # The remaining branches only matter while egress is active.
    if not m.livestream_egress_id:
        return 0

    # 1. Rotation: if broadcast has been live long enough, create+bind
    # a new one and transition the old to complete.
    started = m.livestream_youtube_broadcast_started_at
    # SQLite + SQLAlchemy hand back naive datetimes even when the column
    # was written as tz-aware (the DateTime(timezone=True) flag is a
    # type-decorator hint, not real storage). Coerce to UTC so the
    # subtraction below doesn't crash — without this the supervisor
    # raises TypeError every tick and broadcasts never rotate at the
    # 12 h YouTube cap, silently aging the stream off-air.
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (
        started
        and m.livestream_youtube_broadcast_id
        and (datetime.now(timezone.utc) - started).total_seconds()
        >= _settings.youtube_broadcast_rotate_after_seconds
    ):
        try:
            old_id = m.livestream_youtube_broadcast_id
            new = await create_broadcast(m)
            m.livestream_youtube_broadcast_id = new.broadcast_id
            m.livestream_youtube_watch_url = new.watch_url
            m.livestream_youtube_broadcast_started_at = datetime.now(timezone.utc)
            db.commit()
            changes += 1
            # Try to transition the new broadcast to live (the
            # upstream RTMP is still flowing through the same key, so
            # YouTube should accept immediately). Then complete the
            # previous broadcast.
            try:
                await transition_broadcast(m, to="live")
            except YouTubeLiveError:
                log.warning("supervise: rotation new→live deferred")
            # Re-point m to the OLD broadcast just for the complete()
            # call, then restore. Cleaner than threading the id through.
            try:
                m.livestream_youtube_broadcast_id = old_id
                await complete_broadcast(m)
            finally:
                m.livestream_youtube_broadcast_id = new.broadcast_id
            db.commit()
        except Exception:
            log.exception("supervise: rotation failed")

    # 3. Lifecycle observability only — the supervisor used to retry
    # transition→live every tick, but in production that turned out to
    # be futile:
    #   • For the FIRST broadcast of a session, `enableAutoStart=True`
    #     (set in create_broadcast) flips ready→live the moment YouTube
    #     receives the first bytes. The supervisor's transition call
    #     was unnecessary belt-and-suspenders.
    #   • For ROTATION broadcasts (we create a fresh one at ~11h30m
    #     while the egress keeps streaming continuously to the same
    #     RTMP key), autoStart doesn't fire because YouTube never sees
    #     a "new" stream connection — and a direct transition request
    #     returns HTTP 403 `invalidTransition`. The retry just burns
    #     ~50 quota units per tick (144 k/day at 30 s) and never
    #     succeeds.
    #
    # We keep the cheap (1-unit) lifecycle peek so the log captures
    # when a broadcast is stuck or has died on YouTube's side. Fixing
    # rotation properly needs an egress pause/resume around the bind
    # — a separate, larger change.
    if m.livestream_youtube_broadcast_id:
        lifecycle = await get_broadcast_lifecycle(m)
        if lifecycle in ("complete", "revoked"):
            log.warning(
                "supervise: broadcast %s on meeting %s is %s — no transitions until rotation",
                m.livestream_youtube_broadcast_id, m.id, lifecycle,
            )
        elif lifecycle in ("ready", "testing"):
            # Stuck broadcast (rotation case described above) — not an
            # error per se, but worth a debug breadcrumb so the host can
            # tell whether the supervisor noticed.
            log.debug(
                "supervise: broadcast %s on meeting %s is %s (autoStart will promote on next stream connection)",
                m.livestream_youtube_broadcast_id, m.id, lifecycle,
            )

    # 4. Viewer count + health snapshot to LivestreamDestinationState.
    if m.livestream_youtube_broadcast_id:
        viewers = await get_concurrent_viewers(m)
        row = (
            db.query(LivestreamDestinationState)
            .filter_by(meeting_id=m.id, platform_id="youtube")
            .first()
        )
        if row is None:
            row = LivestreamDestinationState(meeting_id=m.id, platform_id="youtube", status="streaming")
            db.add(row)
        if viewers is not None:
            row.viewer_count = viewers
            row.viewer_count_at = datetime.now(timezone.utc)
            changes += 1
        db.commit()

    return changes


async def supervise_all(db: Any) -> int:
    """Walk every meeting whose YouTube API mode is currently relevant
    (either egress is running, or a broadcast is still flagged) and run
    the per-meeting supervisor tick. Returns the aggregated change count
    so the scheduler can log it."""
    from app.models import Meeting as _Meeting

    total = 0
    q = (
        db.query(_Meeting)
        .filter(_Meeting.livestream_youtube_enabled.is_(True))
        .filter(_Meeting.livestream_youtube_mode == "api")
        .filter(_Meeting.livestream_youtube_refresh_token.is_not(None))
    )
    for m in q.all():
        try:
            total += await supervise_meeting(m, db)
        except Exception:
            log.exception("supervise_all: meeting %s tick failed", m.id)
    return total


async def get_concurrent_viewers(m: Meeting) -> int | None:
    """Returns concurrent viewers for the meeting's current broadcast, or
    None if YouTube hasn't computed one yet (broadcast not live, or in
    the first ~10s before stats settle)."""
    if not m.livestream_youtube_refresh_token or not m.livestream_youtube_broadcast_id:
        return None
    try:
        at = await _access_token(m.livestream_youtube_refresh_token)
        body = await _api(
            at, "GET", "/videos",
            params={"part": "liveStreamingDetails", "id": m.livestream_youtube_broadcast_id},
        )
    except YouTubeLiveError as e:
        log.warning("viewer count fetch failed: %s", str(e)[:200])
        return None
    items = body.get("items") or []
    if not items:
        return None
    details = items[0].get("liveStreamingDetails") or {}
    raw = details.get("concurrentViewers")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
