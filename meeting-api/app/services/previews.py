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

# ─── Share-card layout ─────────────────────────────────────────────────────
# Bump when the poster composition changes: the version is part of the cache
# filename and the routes' media URLs, so social platforms refetch.
_CARD_VERSION = 7
# Candidate frame times for the card image. The configured preferred second
# (default 5s) is tried first; the rest are fallbacks for videos whose
# opening seconds are black or fading — a fixed 5s frame produced solid
# black Facebook/WhatsApp cards in production.
_CANDIDATE_TIMES: tuple[float, ...] = (8.0, 12.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0)
_BEST_TIME: dict[str, tuple[float, float]] = {}  # "<path>|<mtime>" -> (mtime, t)

_FONT_BOLD_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)
_FONT_REGULAR_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)

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


def _note_failure(key: str) -> None:
    now = time.time()
    _FAILED[key] = now + _FAILURE_TTL_SECONDS
    if len(_FAILED) > 2000:
        for k, expiry in list(_FAILED.items()):
            if expiry <= now:
                _FAILED.pop(k, None)


def _in_failure_cooldown(key: str) -> bool:
    expiry = _FAILED.get(key)
    if expiry is None:
        return False
    if expiry <= time.time():
        _FAILED.pop(key, None)
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
    by HEAD probes to answer without spawning ffmpeg. The layout version is
    part of the name so a composition change invalidates old cards."""
    return (
        _cache_root()
        / f"{_safe_id(item_id)}-poster-{int(t * 1000)}-w{width}-v{_CARD_VERSION}.jpg"
    )


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


def find_cached(item_id: str, kind: str) -> Path | None:
    """Newest cached artifact of `kind` ("poster", "gif", "clip") for an
    item, regardless of which frame time it was generated at. Lets HEAD
    probes answer from disk without re-deriving the chosen time."""
    prefix = f"{_safe_id(item_id)}-{kind}-"
    best: tuple[float, Path] | None = None
    try:
        for p in _cache_root().glob(f"{prefix}*"):
            if not p.is_file() or ".tmp." in p.name or ".frame." in p.name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, p)
    except OSError:
        return None
    return best[1] if best else None


def _fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _font(paths: tuple[str, ...], size: int):
    from PIL import ImageFont

    for path in paths:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _wrap_lines(draw, text: str, font, max_width: float, max_lines: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    cur = ""
    for word in words:
        probe = f"{cur} {word}".strip()
        if cur and draw.textlength(probe, font=font) > max_width:
            lines.append(cur)
            cur = word
        else:
            cur = probe
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _probe_frame_jpeg(src: Path, t: float) -> bytes | None:
    """Extract one small frame at `t` as JPEG bytes (for scoring)."""
    cmd = [
        _FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{max(0.0, t):.3f}",
        "-i", str(src),
        "-frames:v", "1",
        "-vf", "scale=192:-2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "5",
        "pipe:1",
    ]
    try:
        with _GEN_SEMAPHORE:
            proc = subprocess.run(cmd, capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _frame_score(data: bytes) -> float:
    """Heuristic scene score for a candidate frame. Rewards brightness,
    texture and color; heavily penalizes black/fade frames and slide-like
    frames (dark or white background covered in text) — a fair-use/title
    card is bright but makes an ugly card."""
    from io import BytesIO

    from PIL import Image, ImageStat

    try:
        img = Image.open(BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return -1.0
    luma = img.convert("L")
    hist = luma.histogram()
    n = max(1, luma.width * luma.height)
    mean = sum(i * c for i, c in enumerate(hist)) / n
    var = sum((i - mean) ** 2 * c for i, c in enumerate(hist)) / n
    std = var ** 0.5
    # Tolerant thresholds: low-quality JPEG probes smear text edges.
    dark = sum(hist[:40]) / n
    bright = sum(hist[215:]) / n
    sat = ImageStat.Stat(img.convert("HSV")).mean[1]

    score = mean * 0.4 + std * 1.1 + sat * 0.7
    if mean < 24:  # black / fade
        score *= 0.15
    if dark > 0.35 and bright > 0.02:  # dark slide with bright text
        score *= 0.15
    if bright > 0.30 and sat < 50:  # mostly-white slide
        score *= 0.35
    return score


def pick_frame_time(src: Path, duration: float) -> float:
    """Choose the best-looking frame time for the card and clips.

    Candidates start at `on_demand_poster_time_seconds` (5s default) and
    include later offsets plus 5%/10% of the duration (to escape long
    intros/slides). Each candidate is scored by `_frame_score` and the best
    one wins. Memoized per (path, mtime) so poster/GIF/clip agree and
    repeat requests don't re-probe."""
    try:
        mtime = src.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = f"{src}|{int(mtime)}"
    cached = _BEST_TIME.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    preferred = float(settings.on_demand_poster_time_seconds)
    limit = max(1.0, duration - 1.0)
    raw = [preferred, *_CANDIDATE_TIMES, duration * 0.05, duration * 0.10]
    times: list[float] = []
    for t in raw:
        t = min(max(1.0, t), limit)
        if all(abs(t - seen) > 0.5 for seen in times):
            times.append(t)

    best_score = -1.0
    chosen = min(preferred, limit)
    for t in times:
        data = _probe_frame_jpeg(src, t)
        if data is None:
            continue
        s = _frame_score(data)
        if s > best_score:
            best_score = s
            chosen = t
    if len(_BEST_TIME) > 1024:
        _BEST_TIME.clear()
    _BEST_TIME[key] = (mtime, chosen)
    return chosen


def _compose_card(
    frame_path: Path, out: Path, title: str, subtitle: str, duration: float
) -> bool:
    """Compose the 1200x630 social card: a full-bleed (or blurred-background)
    frame, bottom gradient, title/subtitle and a duration chip. Returns
    False when Pillow fails — the caller then serves the raw frame."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
    except Exception:  # noqa: BLE001 — Pillow missing should degrade, not 500
        return False

    W, H = 1200, 630
    try:
        frame = Image.open(frame_path).convert("RGB")

        # Landscape sources are cover-cropped full-bleed (a 4:3 recording
        # often carries baked letterbox bars that would otherwise show as a
        # second set of bars). Tall/portrait sources keep the whole frame,
        # over a blurred cover background.
        aspect = frame.width / max(1, frame.height)
        if aspect >= 1.2:
            card = ImageOps.fit(
                frame, (W, H), method=Image.LANCZOS, centering=(0.5, 0.45)
            )
        else:
            bg = ImageOps.fit(
                frame, (W, H), method=Image.LANCZOS, centering=(0.5, 0.4)
            )
            bg = bg.filter(ImageFilter.GaussianBlur(26))
            bg = ImageEnhance.Brightness(bg).enhance(0.45)
            card = bg.copy()
            fg = ImageOps.contain(frame, (W, H), method=Image.LANCZOS)
            card.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        grad_start = int(H * 0.35)
        for y in range(grad_start, H):
            f = (y - grad_start) / max(1, H - grad_start)
            od.line([(0, y), (W, y)], fill=(4, 9, 18, int(235 * f * f)))
        # Frosted caption bar: blur the region so lower thirds, credits or
        # subtitles burned into the frame can't be read behind our text,
        # then darken it for contrast.
        blurred = card.filter(ImageFilter.GaussianBlur(14))
        bar_mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(bar_mask).rounded_rectangle(
            [28, H - 240, W - 28, H - 24], radius=18, fill=255
        )
        card.paste(blurred, (0, 0), bar_mask)
        od.rounded_rectangle(
            [28, H - 240, W - 28, H - 24], radius=18, fill=(4, 9, 18, 150)
        )
        card = Image.alpha_composite(card.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(card)

        font_kick = _font(_FONT_BOLD_PATHS, 24)
        font_title = _font(_FONT_BOLD_PATHS, 52)
        font_sub = _font(_FONT_REGULAR_PATHS, 28)
        font_chip = _font(_FONT_BOLD_PATHS, 26)

        x = 56
        y = H - 222
        draw.text(
            (x, y),
            "ON DEMAND · MEET.WITYSK.ORG",
            font=font_kick,
            fill=(56, 189, 248, 255),
        )
        y += 40
        for line in _wrap_lines(draw, title, font_title, W - x - 220, 2):
            draw.text((x, y), line, font=font_title, fill=(246, 250, 255, 255))
            y += 60
        if subtitle:
            sub_lines = _wrap_lines(draw, subtitle, font_sub, W - x - 220, 1)
            if sub_lines:
                draw.text(
                    (x, y + 8), sub_lines[0], font=font_sub, fill=(203, 213, 225, 255)
                )

        if duration > 0:
            label = _fmt_duration(duration)
            text_w = draw.textlength(label, font=font_chip)
            bx = W - 56 - text_w - 28
            by = H - 92
            draw.rounded_rectangle(
                [bx, by, W - 56, by + 46], radius=10, fill=(6, 12, 24, 200)
            )
            draw.text(
                (bx + 14, by + 8), label, font=font_chip, fill=(255, 255, 255, 255)
            )

        card.convert("RGB").save(out, "JPEG", quality=88)
        return out.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        log.warning("card composition failed: %s", e)
        out.unlink(missing_ok=True)
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
        _note_failure(str(out))
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
    src: Path,
    item_id: str,
    duration: float,
    title: str = "",
    subtitle: str = "",
    width: int = 1200,
) -> Path:
    """1200x630 branded social card.

    Picks the best usable frame near the configured second (skipping
    black/fade openings), then composes it with a blurred cover background,
    play badge, title, subtitle and duration chip. Falls back to the raw
    frame when Pillow composition fails."""
    kind_key = f"{_safe_id(item_id)}:poster"
    if _in_failure_cooldown(kind_key):
        raise PreviewError("recent poster failure; backing off")
    t = pick_frame_time(src, duration)
    out = poster_cache_path(item_id, t, width)
    if _fresh(out, src):
        return out
    with _lock_for(str(out)):
        if _fresh(out, src):
            return out
        try:
            # Accurate-ish seek: land 0.5s before target on a keyframe,
            # then decode forward. Costs little and avoids a frame off.
            pre = max(0.0, t - 0.5)
            args = [
                "-ss", f"{pre:.3f}",
                "-i", str(src),
                "-ss", f"{t - pre:.3f}",
                "-frames:v", "1",
                "-vf", f"scale={width}:-2:flags=lanczos",
                "-q:v", "2",
            ]
            frame = out.with_name(out.stem + ".frame" + out.suffix)
            _run_ffmpeg(args, frame, timeout=45)
            if not _compose_card(frame, out, title, subtitle, duration):
                try:
                    os.replace(frame, out)
                except OSError as e:
                    frame.unlink(missing_ok=True)
                    raise PreviewError(f"could not store poster: {e}") from e
            frame.unlink(missing_ok=True)
        except PreviewError:
            _note_failure(kind_key)
            raise
    return out


def gif_path(
    src: Path,
    item_id: str,
    source_duration: float,
    clip_seconds: float = 3.0,
    width: int = 320,
    fps: int = 8,
    start: float | None = None,
) -> Path:
    """Short animated GIF starting at a bright frame.

    Intentionally a single-pass encode with the GIF muxer's own palette:
    `palettegen`/`paletteuse` from a large HEVC source buffers frames faster
    than the encoder drains them and allocated >3 GB on a real 2048x1080
    production file. The plain pipeline stays under ~150 MB while still
    producing a serviceable preview (social platforms usually show only the
    first frame anyway)."""
    kind_key = f"{_safe_id(item_id)}:gif"
    if _in_failure_cooldown(kind_key):
        raise PreviewError("recent gif failure; backing off")
    if start is None:
        start = pick_frame_time(src, source_duration)
    duration = min(clip_seconds, max(0.5, source_duration - start))
    out = gif_cache_path(item_id, start, duration, width, fps)
    if _fresh(out, src):
        return out
    with _lock_for(str(out)):
        if _fresh(out, src):
            return out
        try:
            args = [
                "-ss", f"{max(0.0, start):.3f}",
                "-i", str(src),
                "-t", f"{max(0.5, duration):.3f}",
                "-an",
                "-vf", f"fps={fps},scale={width}:-2:flags=bilinear",
                "-loop", "0",
            ]
            _run_ffmpeg(args, out, timeout=45)
        except PreviewError:
            _note_failure(kind_key)
            raise
    return out


def clip_path(
    src: Path,
    item_id: str,
    source_duration: float,
    clip_seconds: float = 8.0,
    width: int = 1280,
    start: float | None = None,
) -> Path:
    """Short H.264/AAC MP4 (faststart) used as og:video and as the
    twitter:player:stream source. Starts at a bright frame so platforms'
    inline players don't open on a black still."""
    kind_key = f"{_safe_id(item_id)}:clip"
    if _in_failure_cooldown(kind_key):
        raise PreviewError("recent clip failure; backing off")
    if start is None:
        start = pick_frame_time(src, source_duration)
    duration = min(clip_seconds, max(0.5, source_duration - start))
    out = clip_cache_path(item_id, start, duration, width)
    if _fresh(out, src):
        return out
    with _lock_for(str(out)):
        if _fresh(out, src):
            return out
        try:
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
        except PreviewError:
            _note_failure(kind_key)
            raise
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
