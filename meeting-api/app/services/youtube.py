"""
YouTube manual publishing for finished recordings.

Uses an OAuth2 desktop client + a long-lived refresh token. The owner of the
YouTube channel must perform the consent flow once locally (see
docs/youtube-setup.md) to obtain the refresh token; from then on the server
exchanges it for short-lived access tokens automatically.

Required env (all in /opt/meet/.env):
  YOUTUBE_CLIENT_ID       — from Google Cloud Console > OAuth 2.0 Client IDs
  YOUTUBE_CLIENT_SECRET   — from the same client
  YOUTUBE_REFRESH_TOKEN   — issued during the one-time consent flow
  YOUTUBE_DEFAULT_PRIVACY — "unlisted" (default) | "public" | "private"

Returns a dict on success:
  {"video_id": "...", "url": "https://youtu.be/..."}

Raises YouTubeError on any failure with a human-readable message.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
from typing import Iterator

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class YouTubeError(Exception):
    pass


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    url: str


def _need(name: str, value: str) -> str:
    if not value:
        raise YouTubeError(f"{name} is not configured in /opt/meet/.env")
    return value


async def _exchange_refresh_token() -> str:
    """Trade the long-lived refresh_token for a short-lived access_token."""
    cid = _need("YOUTUBE_CLIENT_ID", settings.youtube_client_id)
    cs = _need("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret)
    rt = _need("YOUTUBE_REFRESH_TOKEN", settings.youtube_refresh_token)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cid,
                "client_secret": cs,
                "refresh_token": rt,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise YouTubeError(f"refresh token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def _file_chunks(path: Path, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                return
            yield buf


async def upload_recording(
    *,
    file_path: Path,
    title: str,
    description: str,
    privacy: str | None = None,
) -> UploadResult:
    """Upload a local MP4 to YouTube via the resumable upload protocol.

    Returns (video_id, public-facing URL). Raises YouTubeError on any failure.
    """
    if not file_path.exists():
        raise YouTubeError(f"file not found: {file_path}")
    size = file_path.stat().st_size
    if size == 0:
        raise YouTubeError("file is empty")

    privacy_status = (privacy or settings.youtube_default_privacy or "unlisted").lower()
    if privacy_status not in ("public", "unlisted", "private"):
        raise YouTubeError(f"invalid privacy {privacy_status!r}")

    access_token = await _exchange_refresh_token()

    metadata = {
        "snippet": {
            "title": title[:100],  # YouTube limit
            "description": description[:5000],
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    timeout = httpx.Timeout(connect=30, read=300, write=300, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. initiate the resumable upload
        init = await client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=metadata,
        )
        if init.status_code != 200:
            raise YouTubeError(
                f"resumable init failed: HTTP {init.status_code} {init.text[:300]}"
            )
        upload_url = init.headers.get("Location")
        if not upload_url:
            raise YouTubeError("server did not return resumable upload URL")

        # 2. send the bytes in one PUT (httpx will stream the file).
        with file_path.open("rb") as f:
            put = await client.put(
                upload_url,
                content=f.read(),  # for large files (>1GB) switch to chunked PUT
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                },
            )
        if put.status_code not in (200, 201):
            raise YouTubeError(
                f"upload failed: HTTP {put.status_code} {put.text[:300]}"
            )

        body = put.json()
        video_id = body.get("id")
        if not video_id:
            raise YouTubeError(f"no video id in response: {body}")

    log.info("YOUTUBE_UPLOAD_OK video_id=%s privacy=%s size=%d title=%r", video_id, privacy_status, size, title)
    return UploadResult(video_id=video_id, url=f"https://youtu.be/{video_id}")
