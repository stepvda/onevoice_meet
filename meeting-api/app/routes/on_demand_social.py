"""Social share cards for On Demand videos.

`https://meet.witysk.org/on-demand?v=<item_id>` is a client-rendered SPA
route, so social crawlers (X/Twitter, Facebook, WhatsApp, Telegram, Slack,
Discord, LinkedIn, …) that never execute JavaScript would otherwise see the
generic site card. Caddy forwards that exact path here, and we serve the
*very same* built SPA shell with per-video meta tags injected into <head> —
so crawlers read a rich card and browsers still boot the normal app.

The same module serves the preview media (frame at ~5s, animated GIF, short
MP4 clip), a framable `/embed/on-demand` player page (twitter:player /
oEmbed) and an oEmbed JSON endpoint. Visibility is gated by the exact same
`resolve_on_demand_item` helper the public stream route uses.
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
from datetime import timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_db
from app.models import Meeting, PlaybackItem
from app.routes.playback import resolve_on_demand_item
from app.services import previews
from app.services.whats_next_slide import clean_title

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")

# 1200x630 is the ratio social platforms render as a large card.
POSTER_WIDTH = 1200
POSTER_HEIGHT = 630
# Bump when card content changes so platforms refetch (the endpoints ignore
# unknown query params and the internal cache filename is versioned too).
MEDIA_URL_VERSION = 3
# The other two formats the share page advertises. Kept fixed — the media
# endpoints no longer expose per-request time/width parameters, because an
# anonymous caller could otherwise force unbounded distinct ffmpeg encodes
# (crawler-triggered availability risk on the small production host).
GIF_WIDTH = 320
GIF_DURATION = 3.0
CLIP_WIDTH = 1280
CLIP_DURATION = 8.0

# ─── SPA shell discovery ───────────────────────────────────────────────────

_SHELL_CACHE: dict[str, object] = {"path": None, "mtime": 0.0, "text": None}


def _load_shell() -> str | None:
    """Read `/srv/frontend/index.html` (the built SPA shell) with an
    mtime-keyed in-memory cache. Returns None when the volume isn't mounted
    (dev, tests) — callers then fall back to a standalone HTML page."""
    path = Path(settings.frontend_dir) / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _SHELL_CACHE["path"] == str(path) and _SHELL_CACHE["mtime"] == mtime:
        return _SHELL_CACHE["text"]  # type: ignore[return-value]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    _SHELL_CACHE.update(path=str(path), mtime=mtime, text=text)
    return text


# Strip the generic site-level tags so the per-video ones win regardless of
# how strictly a crawler picks the "first" tag.
_META_RE = re.compile(
    r'<meta\s+[^>]*?(?:property|name)=["\'](?:og:|twitter:)[^"\']*["\'][^>]*>\s*',
    re.IGNORECASE,
)
_NAME_META_RE = re.compile(
    r'<meta\s+[^>]*?name=["\'](?:description|robots|googlebot|keywords)["\'][^>]*>\s*',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title>.*?</title>\s*", re.IGNORECASE | re.DOTALL)
_CANONICAL_RE = re.compile(
    r'<link\s+[^>]*?rel=["\']canonical["\'][^>]*>\s*', re.IGNORECASE
)


def _inject_meta(shell: str, block: str) -> str:
    """Replace the generic title/description/OG/Twitter tags in the SPA
    shell with `block`, inserted just before </head>."""
    shell = _TITLE_RE.sub("", shell)
    shell = _CANONICAL_RE.sub("", shell)
    shell = _META_RE.sub("", shell)
    shell = _NAME_META_RE.sub("", shell)
    if "</head>" in shell:
        return shell.replace("</head>", f"{block}\n  </head>", 1)
    return f"{block}\n{shell}"


# ─── Formatting helpers ────────────────────────────────────────────────────


def fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def iso_duration(seconds: float) -> str:
    """ISO-8601 duration (PT#H#M#S) for schema.org VideoObject."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    out = "PT"
    if h:
        out += f"{h}H"
    if m:
        out += f"{m}M"
    if sec or out == "PT":
        out += f"{sec}S"
    return out


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _jsonld_escape(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


# ─── Context ───────────────────────────────────────────────────────────────


def _video_context(item: PlaybackItem, meeting: Meeting, duration: float) -> dict:
    title = clean_title(item.filename)
    meeting_title = meeting.display_title or meeting.room_name
    host = (meeting.owner_name or "").strip()
    host_clause = f", hosted by {host}" if host else ""
    share_url = f"{settings.public_url}/on-demand?v={item.id}"
    base = f"{settings.public_url}/api/v1/on-demand/items/{item.id}"
    desc = (
        f"Watch “{title}” on demand — {meeting_title}{host_clause}. "
        f"{fmt_duration(duration)} video, free and no account needed."
    )
    return {
        "kind": "video",
        "title": title,
        "page_title": f"{title} — {meeting_title}",
        "owner_name": host,
        "description": desc,
        "duration": duration,
        "share_url": share_url,
        "poster_url": f"{base}/poster.jpg?v={MEDIA_URL_VERSION}",
        "clip_url": f"{base}/preview.mp4?v={MEDIA_URL_VERSION}",
        "stream_url": f"{base}",
        "stream_url": f"{base}",
        "embed_url": f"{settings.public_url}/embed/on-demand?v={item.id}",
        "uploaded_at": item.uploaded_at,
    }


def _generic_context() -> dict:
    return {
        "kind": "site",
        "title": "On Demand — Free public videos",
        "page_title": "On Demand — Free public videos",
        "description": (
            "Watch public meet.witysk.org videos on demand — talks, meetings "
            "and livestream replays. Free, browser-based, no account needed."
        ),
        "share_url": f"{settings.public_url}/on-demand",
        "poster_url": f"{settings.public_url}/og-image.jpg",
    }


def _meta_block(ctx: dict) -> str:
    """The <head> fragment injected for the share page."""
    title = _e(ctx["page_title"])
    desc = _e(ctx["description"])
    url = _e(ctx["share_url"])
    image = _e(ctx["poster_url"])
    tags: list[str] = [
        f"<title>{title} | meet.witysk.org</title>",
        f'<meta name="description" content="{desc}" />',
        (
            '<meta name="robots" content="index, follow, max-image-preview:large, '
            'max-video-preview:-1" />'
        ),
        f'<link rel="canonical" href="{url}" />',
    ]
    if ctx["kind"] == "video":
        tags += [
            '<meta property="og:type" content="video.other" />',
            f'<meta property="og:title" content="{title}" />',
            f'<meta property="og:description" content="{desc}" />',
            f'<meta property="og:url" content="{url}" />',
            f'<meta property="og:image" content="{image}" />',
            f'<meta property="og:image:secure_url" content="{image}" />',
            '<meta property="og:image:type" content="image/jpeg" />',
            f'<meta property="og:image:width" content="{POSTER_WIDTH}" />',
            f'<meta property="og:image:height" content="{POSTER_HEIGHT}" />',
            f'<meta property="og:image:alt" content="{title}" />',
            # Some messaging apps prefer a shorter looping image; harmless
            # for the ones that ignore unknown variants.
            f'<meta property="og:image:url" content="{image}" />',
            f'<meta property="og:video" content="{_e(ctx["clip_url"])}" />',
            f'<meta property="og:video:secure_url" content="{_e(ctx["clip_url"])}" />',
            '<meta property="og:video:type" content="video/mp4" />',
            f'<meta name="twitter:card" content="{_e(settings.on_demand_twitter_card)}" />',
            f'<meta name="twitter:title" content="{title}" />',
            f'<meta name="twitter:description" content="{desc}" />',
            f'<meta name="twitter:image" content="{image}" />',
            f'<meta name="twitter:image:alt" content="{title}" />',
            # Player-card tags (used once X whitelists the domain; ignored
            # otherwise, in which case the summary_large_image fields above
            # still produce a rich card).
            f'<meta name="twitter:player" content="{_e(ctx["embed_url"])}" />',
            '<meta name="twitter:player:width" content="1280" />',
            '<meta name="twitter:player:height" content="720" />',
            f'<meta name="twitter:player:stream" content="{_e(ctx["clip_url"])}" />',
            '<meta name="twitter:player:stream:content_type" content="video/mp4" />',
            f'<link rel="alternate" type="application/json+oembed" '
            f'href="{_e(oembed_href(ctx["share_url"]))}" title="{title}" />',
        ]
    else:
        tags += [
            '<meta property="og:type" content="website" />',
            f'<meta property="og:title" content="{title}" />',
            f'<meta property="og:description" content="{desc}" />',
            f'<meta property="og:url" content="{url}" />',
            f'<meta property="og:image" content="{image}" />',
            f'<meta property="og:image:secure_url" content="{image}" />',
            '<meta property="og:image:width" content="1200" />',
            '<meta property="og:image:height" content="630" />',
            f'<meta property="og:image:alt" content="{title}" />',
            '<meta name="twitter:card" content="summary_large_image" />',
            f'<meta name="twitter:title" content="{title}" />',
            f'<meta name="twitter:description" content="{desc}" />',
            f'<meta name="twitter:image" content="{image}" />',
        ]
    tags.append('<meta property="og:site_name" content="meet.witysk.org" />')
    tags.append('<meta property="og:locale" content="en_US" />')
    if ctx["kind"] == "video":
        tags.append(_video_jsonld(ctx))
    return "    " + "\n    ".join(tags)


def oembed_href(share_url: str) -> str:
    return (
        f"{settings.public_url}/api/v1/on-demand/oembed"
        f"?url={urllib.parse.quote(share_url, safe='')}&format=json"
    )


def _video_jsonld(ctx: dict) -> str:
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": ctx["title"],
        "description": ctx["description"],
        "thumbnailUrl": [ctx["poster_url"]],
        "duration": iso_duration(ctx["duration"]),
        "contentUrl": ctx["stream_url"],
        "embedUrl": ctx["embed_url"],
        "url": ctx["share_url"],
        "publisher": {
            "@type": "Organization",
            "name": "TI One Voice",
            "url": "https://one.witysk.org/",
        },
        "isFamilyFriendly": True,
    }
    uploaded = ctx.get("uploaded_at")
    if uploaded is not None:
        if uploaded.tzinfo is None:
            uploaded = uploaded.replace(tzinfo=timezone.utc)
        payload["uploadDate"] = uploaded.isoformat()
    return f'<script type="application/ld+json">{_jsonld_escape(payload)}</script>'


def _fallback_page(ctx: dict) -> str:
    """Standalone HTML page used when the SPA shell isn't mounted. Still
    carries the full card so crawlers work; humans get a link to the app."""
    block = _meta_block(ctx)
    share = _e(ctx["share_url"])
    title = _e(ctx["title"])
    desc = _e(ctx["description"])
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="UTF-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"{block}\n"
        "<style>body{background:#0b1220;color:#e2e8f0;font-family:system-ui,"
        "sans-serif;display:flex;min-height:100vh;align-items:center;"
        "justify-content:center;margin:0}.c{max-width:42rem;padding:2rem}"
        "a{color:#38bdf8}</style>\n"
        "</head>\n<body><div class=\"c\">"
        f"<h1>{title}</h1><p>{desc}</p>"
        f'<p><a href="{share}">Watch on meet.witysk.org</a></p>'
        "</div></body>\n</html>\n"
    )


# ─── HTML builders ─────────────────────────────────────────────────────────


def _render_share_page(v: str | None, db: Session) -> str:
    ctx: dict
    if v:
        try:
            item, meeting, _source, _path, dur = resolve_on_demand_item(v, db)
            ctx = _video_context(item, meeting, dur)
        except HTTPException:
            ctx = _generic_context()
    else:
        ctx = _generic_context()
    block = _meta_block(ctx)
    shell = _load_shell()
    if shell is not None:
        return _inject_meta(shell, block)
    return _fallback_page(ctx)


def _render_embed(ctx: dict) -> str:
    title = _e(ctx["title"])
    poster = _e(ctx["poster_url"])
    share = _e(ctx["share_url"])
    stream = _e(ctx["stream_url"])
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="UTF-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, '
        'viewport-fit=cover" />\n'
        f"<title>{title}</title>\n"
        f'<link rel="canonical" href="{_e(ctx["share_url"])}" />\n'
        "<style>html,body{margin:0;height:100%;background:#000}"
        "video{width:100%;height:100%;object-fit:contain;display:block;background:#000}"
        "a{color:#38bdf8}</style>\n"
        "</head>\n<body>\n"
        f'<video controls autoplay muted playsinline preload="metadata" '
        f'poster="{poster}" src="{stream}"></video>\n'
        f'<noscript><p style="color:#e2e8f0;font-family:sans-serif;padding:1rem">'
        f'<a href="{share}">Watch on meet.witysk.org</a></p></noscript>\n'
        "</body>\n</html>\n"
    )


def _embed_unavailable() -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"UTF-8\" />"
        "<title>Video unavailable</title></head>\n"
        '<body style="background:#000;color:#e2e8f0;font-family:sans-serif;'
        'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
        "<p>This video is no longer available.</p></body></html>\n"
    )


# ─── Routes ────────────────────────────────────────────────────────────────


@router.get("/on-demand/page", response_class=HTMLResponse)
@router.head("/on-demand/page", include_in_schema=False)
def on_demand_share_page(
    request: Request,
    v: Annotated[str | None, Query(max_length=64)] = None,
    db: Session = Depends(get_db),
) -> Response:
    """The `/on-demand` HTML document with per-video card meta tags.

    Caddy rewrites `/on-demand?v=…` here. Unknown or ineligible ids fall
    back to the generic On Demand card (HTTP 200) so a stale shared link
    still boots the SPA and lands on a sensible page."""
    body = _render_share_page(v, db)
    headers = {"Cache-Control": "public, max-age=300", "X-Robots-Tag": "all"}
    if request.method == "HEAD":
        # Crawlers commonly probe with HEAD first; answer status/headers
        # without paying for (or transmitting) the body.
        return Response(
            status_code=200, media_type="text/html; charset=utf-8", headers=headers
        )
    return HTMLResponse(body, headers=headers)


@router.get("/on-demand/embed", response_class=HTMLResponse)
@router.head("/on-demand/embed", include_in_schema=False)
def on_demand_embed(
    request: Request,
    v: Annotated[str | None, Query(max_length=64)] = None,
    db: Session = Depends(get_db),
) -> Response:
    """Framable player page used as twitter:player / oEmbed HTML."""
    if not v:
        return _maybe_head(request, HTMLResponse(_embed_unavailable(), status_code=404))
    try:
        item, meeting, _source, _path, dur = resolve_on_demand_item(v, db)
    except HTTPException:
        return _maybe_head(request, HTMLResponse(_embed_unavailable(), status_code=404))
    return _maybe_head(
        request,
        HTMLResponse(
            _render_embed(_video_context(item, meeting, dur)),
            headers={"Cache-Control": "public, max-age=3600"},
        ),
    )


def _maybe_head(request: Request, response: Response) -> Response:
    """Strip the body from an HTML response for HEAD requests. Starlette's
    base Response (unlike FileResponse) does not do this itself."""
    if request.method != "HEAD":
        return response
    return Response(
        status_code=response.status_code,
        media_type=response.media_type,
        headers={
            k: v for k, v in response.headers.items() if k.lower() != "content-length"
        },
    )


def _cache_headers() -> dict[str, str]:
    return {"Cache-Control": "public, max-age=86400"}


def _media_response(path: Path, media_type: str) -> FileResponse:
    return FileResponse(str(path), media_type=media_type, headers=_cache_headers())


def _head_media(item_id: str, kind: str, media_type: str) -> Response:
    """HEAD probe answer: headers for an existing artifact of any variant,
    or an empty 200 with the right media type so crawlers proceed to the
    GET. Never spawns ffmpeg for a body nobody reads."""
    cached = previews.find_cached(item_id, kind)
    if cached is not None:
        return _media_response(cached, media_type)
    return Response(status_code=200, media_type=media_type, headers=_cache_headers())


@router.get("/on-demand/items/{item_id}/poster.jpg")
@router.head("/on-demand/items/{item_id}/poster.jpg", include_in_schema=False)
def on_demand_poster(request: Request, item_id: str) -> Response:
    """1200x630 branded card (og:image / twitter:image): best usable frame
    near the configured second, composed with title, play badge and
    duration. Falls back to a branded placeholder card when previews are
    disabled or the file can't be decoded.

    The DB session is closed before any ffmpeg work starts so a slow encode
    never pins a pooled connection, and the variant is fixed (no
    caller-chosen time/size): the public card only ever uses one variant,
    and an open parameter space would let anonymous callers force unbounded
    distinct encodes."""
    with SessionLocal() as db:
        item, meeting, _source, path, dur = resolve_on_demand_item(item_id, db)
        title = clean_title(item.filename)
        meeting_title = meeting.display_title or ""
    if request.method == "HEAD":
        return _head_media(item_id, "poster", "image/jpeg")
    out: Path | None = None
    if settings.on_demand_previews_enabled:
        try:
            out = previews.poster_path(
                path,
                item_id,
                dur,
                title=title,
                subtitle=meeting_title,
                width=POSTER_WIDTH,
            )
        except previews.PreviewError as e:
            log.warning("poster generation failed for %s: %s", item_id, e)
    if out is None or not out.exists():
        out = Path(settings.on_demand_preview_dir) / f"{item_id}-poster-fallback.jpg"
        if not out.exists():
            previews.write_placeholder(out, title, meeting_title)
        if not out.exists():
            raise HTTPException(status_code=503, detail="preview unavailable")
    return _media_response(out, "image/jpeg")


@router.get("/on-demand/items/{item_id}/preview.gif")
@router.head("/on-demand/items/{item_id}/preview.gif", include_in_schema=False)
def on_demand_preview_gif(request: Request, item_id: str) -> Response:
    """Short animated GIF preview (best-effort: most platforms show only
    its first frame, some messaging apps animate it)."""
    with SessionLocal() as db:
        _item, _m, _source, path, source_dur = resolve_on_demand_item(item_id, db)
    if request.method == "HEAD":
        return _head_media(item_id, "gif", "image/gif")
    if not settings.on_demand_previews_enabled:
        raise HTTPException(status_code=503, detail="previews disabled")
    try:
        out = previews.gif_path(
            path,
            item_id,
            source_dur,
            clip_seconds=GIF_DURATION,
            width=GIF_WIDTH,
        )
    except previews.PreviewError as e:
        log.warning("gif preview failed for %s: %s", item_id, e)
        raise HTTPException(status_code=503, detail="preview unavailable")
    return _media_response(out, "image/gif")


@router.get("/on-demand/items/{item_id}/preview.mp4")
@router.head("/on-demand/items/{item_id}/preview.mp4", include_in_schema=False)
def on_demand_preview_clip(request: Request, item_id: str) -> Response:
    """Short H.264 clip for og:video / twitter:player:stream."""
    with SessionLocal() as db:
        _item, _m, _source, path, source_dur = resolve_on_demand_item(item_id, db)
    if request.method == "HEAD":
        return _head_media(item_id, "clip", "video/mp4")
    if not settings.on_demand_previews_enabled:
        raise HTTPException(status_code=503, detail="previews disabled")
    try:
        out = previews.clip_path(
            path,
            item_id,
            source_dur,
            clip_seconds=CLIP_DURATION,
            width=CLIP_WIDTH,
        )
    except previews.PreviewError as e:
        log.warning("clip preview failed for %s: %s", item_id, e)
        raise HTTPException(status_code=503, detail="preview unavailable")
    return _media_response(out, "video/mp4")


@router.get("/on-demand/oembed")
@router.head("/on-demand/oembed", include_in_schema=False)
def on_demand_oembed(
    request: Request,
    url: Annotated[str | None, Query()] = None,
    v: Annotated[str | None, Query(max_length=64)] = None,
    # Accepted for oEmbed compatibility; JSON is the only format we emit.
    fmt: Annotated[str, Query(alias="format")] = "json",
    db: Session = Depends(get_db),
) -> Response:
    """Minimal oEmbed (video) endpoint, discoverable from the share page."""
    vid = v
    if not vid and url:
        vid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("v", [None])[0]
    if not vid:
        raise HTTPException(status_code=404, detail="no video in request")
    try:
        item, meeting, _source, _path, dur = resolve_on_demand_item(vid, db)
    except HTTPException:
        raise HTTPException(status_code=404, detail="video not found")
    ctx = _video_context(item, meeting, dur)
    body = {
        "version": "1.0",
        "type": "video",
        "provider_name": "meet.witysk.org",
        "provider_url": settings.public_url,
        "title": ctx["title"],
        "description": ctx["description"],
        "author_name": ctx["owner_name"] or None,
        "thumbnail_url": ctx["poster_url"],
        "thumbnail_width": POSTER_WIDTH,
        "thumbnail_height": POSTER_HEIGHT,
        "width": 1280,
        "height": 720,
        "html": (
            f'<iframe src="{html.escape(ctx["embed_url"], quote=True)}" '
            'width="1280" height="720" frameborder="0" '
            'allow="autoplay; fullscreen; picture-in-picture" '
            'allowfullscreen title="'
            + html.escape(ctx["title"], quote=True)
            + '"></iframe>'
        ),
    }
    return _maybe_head(
        request,
        JSONResponse(body, headers={"Cache-Control": "public, max-age=3600"}),
    )
