"""On-the-fly social-preview media for On Demand videos.

Generates (and caches on disk) three preview artifacts from a playlist MP4
using the ffmpeg/ffprobe already present in the meeting-api image:

    poster.jpg   1200x630 still taken ~5s in  — og:image / twitter:image
    preview.gif  short animated GIF (~3.5s)    — messaging apps that animate
    preview.mp4  short H.264 clip (~8s)        — og:video / twitter:player:stream

Every artifact is generated at most once per (item, params) pair; concurrent
requests for the same artifact wait on a per-key lock and then hit the cache.
A counting semaphore plus `-threads 1` keeps ffmpeg from starving the API or
a running egress on the 2-vCPU production host. The cache is pruned to
`settings.on_demand_preview_cache_max_bytes` (oldest first) after each
generation so a large catalogue can't fill the root disk.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)


class PreviewError(RuntimeError):
    """Raised when ffmpeg fails to produce a requested preview artifact."""


# The container ships `/usr/local/bin/meet-ffmpeg`, a wrapper that caps the
# data segment (1.5 GiB), CPU time and scheduling priority. Local dev falls
# back to plain ffmpeg. NEVER call ffmpeg directly from here: an unbounded
# palettegen job from a 2048x1080 HEVC source once climbed past 3 GB RSS and
# pushed the production host into global OOM territory.
_FFMPEG_BIN = shutil.which("meet-ffmpeg") or "ffmpeg"

# One preview encode at a time. The box also runs live egress + LiveKit; two
# concurrent transcodes can starve them even with 4 vCPUs.
_GEN_SEMAPHORE = threading.BoundedSemaphore(1)
_DEFAULT_TIMEOUT_SECONDS = 90

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# Negative cache: a failed generation (corrupt file, timeout, memory cap) is
# remembered for a few minutes so crawler retries and sequential requests
# can't respawn ffmpeg for a known-bad source on every hit.
_FAILED: dict[str, float] = {}
_FAILURE_TTL_SECONDS = 300.0
# `_prune_cache` is an O(cache) directory walk; run it at most this often
# rather than on every successful encode.
_PRUNE_INTERVAL_SECONDS = 60.0
_LAST_PRUNE = 0.0


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _note_failure(out: Path) -> None:
    now = time.time()
    _FAILED[str(out)] = now + _FAILURE_TTL_SECONDS
    if len(_FAILED) > 2000:
        for key, expiry in list(_FAILED.items()):
            if expiry <= now:
                _FAILED.pop(key, None)


def _in_failure_cooldown(out: Path) -> bool:
    expiry = _FAILED.get(str(out))
    if expiry is None:
        return False
    if expiry <= time.time():
        _FAILED.pop(str(out), None)
        return False
    return True


def _safe_id(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", item_id)[:80]


def _cache_root() -> Path:
    root = Path(settings.on_demand_preview_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def poster_cache_path(item_id: str, t: float = 5.0, width: int = 1200) -> Path:
    """Where the poster for these parameters is (or would be) cached. Used
    by HEAD probes to answer without spawning ffmpeg."""
    return _cache_root() / f"{_safe_id(item_id)}-poster-{int(t * 1000)}-w{width}.jpg"


def gif_cache_path(
    item_id: str,
    start: float = 5.0,
    duration: float = 3.0,
    width: int = 320,
    fps: int = 8,
) -> Path:
    return _cache_root() / (
        f"{_safe_id(item_id)}-gif-{int(start * 1000)}-{int(duration * 1000)}-"
        f"w{width}-f{fps}.gif"
    )


def clip_cache_path(
    item_id: str, start: float = 5.0, duration: float = 8.0, width: int = 1280
) -> Path:
    return _cache_root() / (
        f"{_safe_id(item_id)}-clip-{int(start * 1000)}-{int(duration * 1000)}-"
        f"w{width}.mp4"
    )


def is_cached(out: Path) -> bool:
    """Non-empty artifact already on disk (no freshness check — callers that
    need mtime validation use the `*_path` functions)."""
    try:
        return out.stat().st_size > 0
    except OSError:
        return False


def _fresh(out: Path, src: Path) -> bool:
    """True when `out` exists, is non-empty and at least as new as the
    source MP4. Playlist files are immutable once uploaded, so an mtime
    comparison is sufficient (and cheap)."""
    try:
        st = out.stat()
        return st.st_size > 0 and st.st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def _run_ffmpeg(args: list[str], out: Path, timeout: int = _DEFAULT_TIMEOUT_SECONDS) -> None:
    """Run ffmpeg and remember failures so known-bad sources back off."""
    try:
        _run_ffmpeg_inner(args, out, timeout)
    except PreviewError:
        _note_failure(out)
        raise


def _run_ffmpeg_inner(args: list[str], out: Path, timeout: int = _DEFAULT_TIMEOUT_SECONDS) -> None:
    """Run ffmpeg (through the resource-capped wrapper when available),
    writing to a sibling tmp file with the real extension (so the muxer can
    still infer the format) then atomically renaming."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    tmp.unlink(missing_ok=True)
    cmd = [
        _FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        *args,
        "-threads", "1",
        str(tmp),
    ]
    try:
        with _GEN_SEMAPHORE:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
    except subprocess.TimeoutExpired as e:
        tmp.unlink(missing_ok=True)
        raise PreviewError(f"ffmpeg timed out after {timeout}s") from e
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise PreviewError(f"ffmpeg could not start: {e}") from e
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        raise PreviewError(f"ffmpeg failed: {tail}")
    try:
        if tmp.stat().st_size <= 0:
            tmp.unlink(missing_ok=True)
            raise PreviewError("ffmpeg produced an empty file")
        os.replace(tmp, out)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise PreviewError(f"could not store preview: {e}") from e
    _prune_cache()


def _prune_cache() -> None:
    """Delete the oldest preview files until the cache is under its cap.
    Best-effort: a racing reader may already hold an fd, which keeps the
    file alive on POSIX until it closes. Throttled — the walk itself is
    O(cache) and must not tax every encode."""
    global _LAST_PRUNE
    now = time.time()
    if now - _LAST_PRUNE < _PRUNE_INTERVAL_SECONDS:
        return
    _LAST_PRUNE = now
    cap = max(64 * 1024 * 1024, int(settings.on_demand_preview_cache_max_bytes))
    root = _cache_root()
    entries: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for p in root.glob("**/*"):
            if not p.is_file():
                continue
            st = p.stat()
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    except OSError:
        return
    if total <= cap:
        return
    entries.sort(key=lambda e: e[0])
    for _, size, p in entries:
        if total <= cap:
            break
        try:
            p.unlink(missing_ok=True)
            total -= size
        except OSError:
            pass


def poster_path(
    src: Path, item_id: str, t: float = 5.0, width: int = 1200
) -> Path:
    """1200x630 JPEG still taken at `t` seconds (center-cropped from the
    scaled frame so any aspect ratio fills the OG box)."""
    height = max(1, round(width * 630 / 1200))
    out = poster_cache_path(item_id, t, width)
    if _fresh(out, src):
        return out
    with _lock_for(str(out)):
        if _fresh(out, src):
            return out
        if _in_failure_cooldown(out):
            raise PreviewError("recent poster failure; backing off")
        # Accurate-ish seek: land 0.5s before target on a keyframe, then
        # decode forward. Costs little and avoids a poster seconds off.
        pre = max(0.0, t - 0.5)
        args = [
            "-ss", f"{pre:.3f}",
            "-i", str(src),
            "-ss", f"{t - pre:.3f}",
            "-frames:v", "1",
            "-vf",
            (
                f"scale={width}:{round(width * 9 / 16)}:"
                f"force_original_aspect_ratio=increase,crop={width}:{height}"
            ),
            "-q:v", "3",
        ]
        _run_ffmpeg(args, out, timeout=45)
    return out


def gif_path(
    src: Path,
    item_id: str,
    start: float = 5.0,
    duration: float = 3.0,
    width: int = 320,
    fps: int = 8,
) -> Path:
    """Short animated GIF.

    Intentionally a single-pass encode with the GIF muxer's own palette:
    `palettegen`/`paletteuse` from a large HEVC source buffers frames faster
    than the encoder drains them and allocated >3 GB on a real 2048x1080
    production file. The plain pipeline stays under ~150 MB while still
    producing a serviceable preview (social platforms usually show only the
    first frame anyway)."""
    out = gif_cache_path(item_id, start, duration, width, fps)
    if _fresh(out, src):
        return out
    with _lock_for(str(out)):
        if _fresh(out, src):
            return out
        if _in_failure_cooldown(out):
            raise PreviewError("recent gif failure; backing off")
        args = [
            "-ss", f"{max(0.0, start):.3f}",
            "-i", str(src),
            "-t", f"{max(0.5, duration):.3f}",
            "-an",
            "-vf", f"fps={fps},scale={width}:-2:flags=bilinear",
            "-loop", "0",
        ]
        _run_ffmpeg(args, out, timeout=45)
    return out


def clip_path(
    src: Path,
    item_id: str,
    start: float = 5.0,
    duration: float = 8.0,
    width: int = 1280,
) -> Path:
    """Short H.264/AAC MP4 (faststart) used as og:video and as the
    twitter:player:stream source."""
    out = clip_cache_path(item_id, start, duration, width)
    if _fresh(out, src):
        return out
    with _lock_for(str(out)):
        if _fresh(out, src):
            return out
        if _in_failure_cooldown(out):
            raise PreviewError("recent clip failure; backing off")
        args = [
            "-ss", f"{max(0.0, start):.3f}",
            "-i", str(src),
            "-t", f"{max(0.5, duration):.3f}",
            "-vf", f"scale={width}:-2",
            "-c:v", "libx264",
            "-profile:v", "main",
            "-preset", "veryfast",
            "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-movflags", "+faststart",
        ]
        _run_ffmpeg(args, out, timeout=90)
    return out


# ─── Pillow fallback card ──────────────────────────────────────────────────


def write_placeholder(out: Path, title: str, subtitle: str = "") -> bool:
    """Branded 1200x630 card used when ffmpeg can't decode the video (or
    isn't available). Returns False when Pillow itself fails — callers then
    surface a 503 rather than a broken image."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001 — Pillow missing should degrade, not 500
        return False

    w, h = 1200, 630
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (w, h), (11, 18, 32))
        draw = ImageDraw.Draw(img)
        # Vertical gradient #0B1220 → #1E3A5F.
        for y in range(h):
            f = y / (h - 1)
            draw.line(
                [(0, y), (w, y)],
                fill=(
                    round(11 + (30 - 11) * f),
                    round(18 + (58 - 18) * f),
                    round(32 + (95 - 32) * f),
                ),
            )
        draw.rectangle([0, 0, w, 8], fill=(56, 189, 248))

        def font(size: int):
            try:
                return ImageFont.load_default(size=size)
            except TypeError:  # Pillow < 10.1
                return ImageFont.load_default()

        title_font = font(58)
        sub_font = font(30)
        brand_font = font(26)

        def wrap(text: str, f, max_w: int) -> list[str]:
            words = text.split()
            lines: list[str] = []
            cur = ""
            for word in words:
                probe = f"{cur} {word}".strip()
                try:
                    width_px = draw.textlength(probe, font=f)
                except Exception:  # noqa: BLE001
                    width_px = len(probe) * (f.size if hasattr(f, "size") else 10)
                if cur and width_px > max_w:
                    lines.append(cur)
                    cur = word
                else:
                    cur = probe
            if cur:
                lines.append(cur)
            return lines[:4]

        title_lines = wrap(title.strip() or "On Demand video", title_font, w - 160)
        y = 170
        for line in title_lines:
            draw.text((80, y), line, font=title_font, fill=(241, 245, 249))
            y += 74
        if subtitle:
            for line in wrap(subtitle, sub_font, w - 160)[:2]:
                draw.text((80, y + 18), line, font=sub_font, fill=(148, 163, 184))
                y += 40
        draw.text((80, h - 64), "meet.witysk.org", font=brand_font, fill=(56, 189, 248))
        img.save(out, "JPEG", quality=86)
        return out.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        log.warning("placeholder card generation failed: %s", e)
        out.unlink(missing_ok=True)
        return False
