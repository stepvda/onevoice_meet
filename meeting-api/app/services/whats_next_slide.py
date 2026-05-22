"""Generate the 35-second "What's up next" rundown slide.

Shown right before any playlist item whose duration > 5 minutes when the
host has enabled the toggle on the Video-playback panel.

Design (broadcast-style bright):
    +---------------------------------------------------------+
    | <meeting name>            WHAT'S UP NEXT  (header band) |
    +---------------------------------------------------------+
    |  [1]  Title One ............................. NEXT      |
    |  [2]  Title Two ............................. in 6m     |
    |  [3]  Title Three ........................... in 18m    |
    |  [4]  Title Four ............................ in 47m    |
    |  [5]  Title Five ............................ in 1h 12m |
    +---------------------------------------------------------+

Each row gets its own accent colour. Underscores in filenames are
turned into spaces and any leading `NN_` numeric prefix is stripped
before display (so `03_TI_OneVoice_Video_20260220.mp4` reads as
`TI OneVoice Video 20260220`).

The slide MP4 carries a 35s upbeat waiting-music bed so the room
isn't silent while the rundown shows.

Caching: rendered MP4s land in `/var/lib/meet/whats_next_cache/` keyed
by a hash of the slide's contents (meeting name + ordered list of
title/offset pairs). Identical content is reused; the cache is
TTL-evicted by the disk-cap job. Pre-generation is fire-and-forget on
toggle-on and after every playlist advance so the encode latency
never gates the actual playback start.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Meeting, PlaybackItem

log = logging.getLogger(__name__)

CACHE_DIR = Path("/var/lib/meet/whats_next_cache")
SLIDE_DURATION_S = 35.0
SLIDE_W, SLIDE_H = 1920, 1080
FPS = 30
ELIGIBLE_MIN_DURATION_S = 300.0  # 5 minutes
MAX_ROWS = 5

# Five accent colours, one per row.
ROW_ACCENT_RGB = [
    (230, 57, 70),    # red       #E63946
    (247, 127, 0),    # orange    #F77F00
    (252, 191, 73),   # amber     #FCBF49
    (6, 167, 125),    # green     #06A77D
    (39, 125, 161),   # blue      #277DA1
]

# Fonts available on both macOS dev box and the Debian-slim container.
# The container ships DejaVu via fontconfig; macOS uses /System fonts.
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _first_existing(paths: list[str]) -> str:
    for p in paths:
        if os.path.exists(p):
            return p
    # Last-resort: PIL default font (tiny bitmap, but lets the module load)
    return ""


# ---------------------------------------------------------------------------
# Title cleaner
# ---------------------------------------------------------------------------

_NN_PREFIX = re.compile(r"^\d+_")


def clean_title(filename: str) -> str:
    """`03_TI_OneVoice_Video_20260220.mp4` -> `TI OneVoice Video 20260220`.

    Strips leading numeric-with-underscore prefixes (handles repeats like
    `03_15_…`), replaces remaining `_` with space, drops the extension."""
    name = Path(filename).stem
    while True:
        m = _NN_PREFIX.match(name)
        if not m:
            break
        name = name[m.end():]
    return name.replace("_", " ").strip() or filename


def _format_offset(seconds: float) -> str:
    """Right-column countdown text. 0 -> NEXT, then minutes / h+m."""
    if seconds <= 1:
        return "NEXT"
    s = int(round(seconds))
    if s < 60:
        return f"in {s} s"
    if s < 60 * 60:
        return f"in {s // 60} min"
    h = s // 3600
    m = (s % 3600) // 60
    return f"in {h}h {m:02d}m"


# ---------------------------------------------------------------------------
# Build the slide data from the playlist
# ---------------------------------------------------------------------------

def build_slide_data(
    db: Session,
    meeting_id: str,
    real_item_id: Optional[str] = None,
) -> Optional[dict]:
    """Return the JSON-shaped data the renderer needs, or None when
    there's nothing to show (no eligible upcoming items).

    `real_item_id` is the playlist item that will start once the slide
    ends — it's the first row of the rundown. If None we infer it from
    `meeting.playback_current_item_id` (used by the pre-gen path)."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not m.playback_enabled or not m.playback_whats_up_next:
        return None
    items = (
        db.query(PlaybackItem)
        .filter_by(meeting_id=meeting_id)
        .order_by(PlaybackItem.position.asc())
        .all()
    )
    if not items:
        return None

    by_id = {it.id: it for it in items}
    sorted_items = items  # already ordered
    n = len(sorted_items)

    # Effective duration: aliases (rows with `source_item_id`) inherit
    # from the source row's value. Historical aliases born while the
    # source's duration was still NULL never got it propagated, so they'd
    # otherwise be silently skipped over.
    def _eff_dur(it: PlaybackItem) -> float:
        if it.duration_seconds is not None:
            return float(it.duration_seconds)
        if it.source_item_id:
            src = by_id.get(it.source_item_id)
            if src is not None and src.duration_seconds is not None:
                return float(src.duration_seconds)
        return 0.0

    # Resolve which item is the "first row of the rundown".
    anchor = None
    if real_item_id:
        anchor = by_id.get(real_item_id)
    if anchor is None and m.playback_current_item_id:
        # Pre-gen path: rundown starts at the next eligible item AFTER
        # the one currently playing.
        cur = by_id.get(m.playback_current_item_id)
        if cur is not None:
            after = [it for it in sorted_items if it.position > cur.position]
            anchor = next(
                (it for it in after if _eff_dur(it) > ELIGIBLE_MIN_DURATION_S),
                None,
            )
            if anchor is None and m.playback_loop:
                anchor = next(
                    (it for it in sorted_items if _eff_dur(it) > ELIGIBLE_MIN_DURATION_S),
                    None,
                )
    if anchor is None:
        # Cold-start pre-gen: the very first eligible item in the list.
        anchor = next(
            (it for it in sorted_items if _eff_dur(it) > ELIGIBLE_MIN_DURATION_S),
            None,
        )
    if anchor is None:
        return None

    # Build the rundown: walk forward from `anchor` (inclusive), wrapping
    # with loop if enabled, capturing up to MAX_ROWS eligible items.
    # Accumulate elapsed time across ALL intervening items so an entry's
    # countdown reflects the real wait, not just eligible-items wait.
    start_idx = sorted_items.index(anchor)
    walk = list(range(start_idx, n))
    if m.playback_loop:
        walk += list(range(0, start_idx))

    rundown: list[dict] = []
    elapsed = 0.0
    for idx in walk:
        it = sorted_items[idx]
        dur = _eff_dur(it)
        if dur > ELIGIBLE_MIN_DURATION_S:
            rundown.append({
                "id": it.id,
                "title": clean_title(it.filename or it.id),
                "offset_seconds": round(elapsed, 1),
            })
            if len(rundown) >= MAX_ROWS:
                break
        elapsed += dur

    if not rundown:
        return None

    name = (m.display_title or "").strip() or "Meeting"
    return {
        "meeting_name": name,
        "anchor_item_id": anchor.id,
        "rundown": rundown,
    }


# ---------------------------------------------------------------------------
# Cache key + paths
# ---------------------------------------------------------------------------

def _cache_key(data: dict) -> str:
    canon = json.dumps(
        {
            "meeting_name": data["meeting_name"],
            "rundown": [
                {"t": r["title"], "o": int(r["offset_seconds"])}
                for r in data["rundown"]
            ],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


def cache_path(data: dict) -> Path:
    return CACHE_DIR / f"{_cache_key(data)}.mp4"


def find_cached(data: dict) -> Optional[Path]:
    p = cache_path(data)
    return p if p.exists() and p.stat().st_size > 0 else None


# ---------------------------------------------------------------------------
# Signed-URL helpers (same pattern as playback_mgr.sign_playback_url)
# ---------------------------------------------------------------------------

_SLIDE_URL_TTL_SECONDS = 30 * 60


def sign_slide_url(slide_key: str) -> str:
    """Returns `<expiry_unix>.<hex32>` — verified by `verify_slide_url`."""
    exp = int(time.time()) + _SLIDE_URL_TTL_SECONDS
    payload = f"whatsnext:{slide_key}:{exp}".encode()
    mac = hmac.new(
        settings.jwt_secret_key.encode(), payload, hashlib.sha256
    ).hexdigest()[:32]
    return f"{exp}.{mac}"


def verify_slide_url(slide_key: str, token: str) -> bool:
    try:
        exp_str, mac = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    payload = f"whatsnext:{slide_key}:{exp}".encode()
    expected = hmac.new(
        settings.jwt_secret_key.encode(), payload, hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(mac, expected)


def build_signed_slide_url(slide_key: str) -> str:
    """Internal-network URL the LiveKit ingress fetches the slide from."""
    base = "http://meeting-api:8080"
    token = sign_slide_url(slide_key)
    return f"{base}/api/v1/internal/whats-next-slide/{slide_key}?token={token}"


def slide_key_from_path(path: Path) -> str:
    return path.stem


def slide_path_for_key(key: str) -> Optional[Path]:
    p = CACHE_DIR / f"{key}.mp4"
    return p if p.exists() and p.stat().st_size > 0 else None


# ---------------------------------------------------------------------------
# Slide PNG renderer
# ---------------------------------------------------------------------------

def _render_slide_png(data: dict, out_path: Path) -> None:
    # Imported lazily so the module loads even before `pip install` runs
    # the new requirements (avoids import-time errors during a rolling
    # deploy).
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    bold_path = _first_existing(_FONT_CANDIDATES_BOLD)
    reg_path = _first_existing(_FONT_CANDIDATES_REGULAR)
    F_HEADER_TITLE = ImageFont.truetype(bold_path, 78) if bold_path else ImageFont.load_default()
    F_HEADER_TAG = ImageFont.truetype(bold_path, 56) if bold_path else ImageFont.load_default()
    F_ROW_NUM = ImageFont.truetype(bold_path, 84) if bold_path else ImageFont.load_default()
    F_ROW_TITLE = ImageFont.truetype(bold_path, 56) if bold_path else ImageFont.load_default()
    F_ROW_OFFSET = ImageFont.truetype(bold_path, 50) if bold_path else ImageFont.load_default()
    F_FOOTER = ImageFont.truetype(reg_path, 28) if reg_path else ImageFont.load_default()

    # ---- Background: warm cream with subtle paper texture ----
    bg = Image.new("RGB", (SLIDE_W, SLIDE_H), (246, 242, 232))
    # Soft top-to-bottom tint
    overlay = Image.new("RGB", (SLIDE_W, SLIDE_H), (255, 255, 255))
    grad = Image.new("L", (1, SLIDE_H))
    for y in range(SLIDE_H):
        # 0 at top -> mild darkening at bottom (255 = transparent overlay)
        grad.putpixel((0, y), int(245 - (y / SLIDE_H) * 30))
    grad = grad.resize((SLIDE_W, SLIDE_H))
    bg = Image.composite(overlay, bg, grad)
    bg = bg.convert("RGB")

    draw = ImageDraw.Draw(bg, "RGBA")

    # ---- Header band (top): vivid red-to-orange gradient ----
    HDR_H = 200
    bg.paste(_horizontal_gradient((SLIDE_W, HDR_H), (230, 57, 70), (247, 127, 0)), (0, 0))
    draw = ImageDraw.Draw(bg, "RGBA")

    # White hairline under the header band
    draw.rectangle([0, HDR_H, SLIDE_W, HDR_H + 4], fill=(255, 255, 255, 230))
    draw.rectangle([0, HDR_H + 4, SLIDE_W, HDR_H + 7], fill=(0, 0, 0, 60))

    # Header text — meeting name (left) + WHAT'S UP NEXT (right)
    meeting_name = data["meeting_name"]
    name_text = _truncate_to_width(draw, meeting_name, F_HEADER_TITLE, SLIDE_W * 0.55)
    draw.text((60, 60), name_text, fill=(255, 255, 255, 255), font=F_HEADER_TITLE)

    tag = "WHAT'S UP NEXT"
    tag_w = draw.textlength(tag, font=F_HEADER_TAG)
    # Soft black plate behind the tag for contrast
    pad_x, pad_y = 26, 14
    plate_w = int(tag_w) + 2 * pad_x
    plate_h = 84
    plate_x = SLIDE_W - plate_w - 60
    plate_y = (HDR_H - plate_h) // 2 + 6
    draw.rounded_rectangle(
        [plate_x, plate_y, plate_x + plate_w, plate_y + plate_h],
        radius=10,
        fill=(20, 20, 30, 200),
    )
    draw.text(
        (plate_x + pad_x, plate_y + pad_y - 6),
        tag,
        fill=(255, 255, 255, 255),
        font=F_HEADER_TAG,
    )

    # ---- Rundown rows ----
    rows = data["rundown"]
    rows_top = HDR_H + 60
    rows_bottom = SLIDE_H - 100
    avail = rows_bottom - rows_top
    row_h = min(150, avail // max(1, len(rows)))
    row_gap = 18
    block_h = row_h * len(rows) + row_gap * (len(rows) - 1)
    # Vertically center the block in the available area
    start_y = rows_top + max(0, (avail - block_h) // 2)

    LEFT_MARGIN = 60
    RIGHT_MARGIN = 60
    NUM_BLOCK_W = 130

    for i, row in enumerate(rows):
        rgb = ROW_ACCENT_RGB[i % len(ROW_ACCENT_RGB)]
        y = start_y + i * (row_h + row_gap)
        # Card background — very pale tint of the accent
        card_fill = _light_tint(rgb, 0.92)
        draw.rounded_rectangle(
            [LEFT_MARGIN, y, SLIDE_W - RIGHT_MARGIN, y + row_h],
            radius=18,
            fill=(*card_fill, 255),
        )
        # Left number block — solid accent
        draw.rounded_rectangle(
            [LEFT_MARGIN, y, LEFT_MARGIN + NUM_BLOCK_W, y + row_h],
            radius=18,
            fill=(*rgb, 255),
        )
        # Cover the right side of the rounded number block so it visually
        # bleeds into the card on the right edge (square cap).
        draw.rectangle(
            [LEFT_MARGIN + NUM_BLOCK_W - 18, y, LEFT_MARGIN + NUM_BLOCK_W, y + row_h],
            fill=(*rgb, 255),
        )
        num_text = str(i + 1)
        nw = draw.textlength(num_text, font=F_ROW_NUM)
        nh = F_ROW_NUM.size
        draw.text(
            (LEFT_MARGIN + (NUM_BLOCK_W - nw) // 2,
             y + (row_h - nh) // 2 - 4),
            num_text,
            fill=(255, 255, 255, 255),
            font=F_ROW_NUM,
        )

        # Title — truncated to remaining width
        title_x = LEFT_MARGIN + NUM_BLOCK_W + 32
        offset_text = _format_offset(row["offset_seconds"])
        offset_w = draw.textlength(offset_text, font=F_ROW_OFFSET)
        offset_x = SLIDE_W - RIGHT_MARGIN - 32 - offset_w
        max_title_w = offset_x - title_x - 30
        title = _truncate_to_width(draw, row["title"], F_ROW_TITLE, max_title_w)
        draw.text(
            (title_x, y + (row_h - F_ROW_TITLE.size) // 2 - 6),
            title,
            fill=(30, 30, 36, 255),
            font=F_ROW_TITLE,
        )

        # Offset — bold accent colour
        # First row gets the "NEXT" emphasis treatment with a colored pill
        if i == 0 and offset_text == "NEXT":
            pill_pad_x = 22
            pill_pad_y = 8
            pill_w = int(offset_w) + 2 * pill_pad_x
            pill_h = F_ROW_OFFSET.size + 2 * pill_pad_y
            pill_x = SLIDE_W - RIGHT_MARGIN - 32 - pill_w
            pill_y = y + (row_h - pill_h) // 2
            draw.rounded_rectangle(
                [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
                radius=12,
                fill=(*rgb, 255),
            )
            draw.text(
                (pill_x + pill_pad_x, pill_y + pill_pad_y - 6),
                offset_text,
                fill=(255, 255, 255, 255),
                font=F_ROW_OFFSET,
            )
        else:
            draw.text(
                (offset_x, y + (row_h - F_ROW_OFFSET.size) // 2 - 6),
                offset_text,
                fill=(*rgb, 255),
                font=F_ROW_OFFSET,
            )

    # ---- Footer hairline ----
    footer_y = SLIDE_H - 60
    draw.rectangle([60, footer_y - 2, SLIDE_W - 60, footer_y], fill=(0, 0, 0, 30))
    footer_text = "TI ONE VOICE TV  ·  Playlist rundown"
    fw = draw.textlength(footer_text, font=F_FOOTER)
    draw.text(
        ((SLIDE_W - fw) // 2, footer_y + 12),
        footer_text,
        fill=(100, 100, 110, 220),
        font=F_FOOTER,
    )

    bg.save(out_path, "PNG", compress_level=6)


def _horizontal_gradient(size, rgb_start, rgb_end):
    """Fast horizontal gradient via a tiny single-row image stretched."""
    from PIL import Image
    w, h = size
    row = Image.new("RGB", (w, 1))
    for x in range(w):
        f = x / max(1, w - 1)
        r = int(rgb_start[0] + (rgb_end[0] - rgb_start[0]) * f)
        g = int(rgb_start[1] + (rgb_end[1] - rgb_start[1]) * f)
        b = int(rgb_start[2] + (rgb_end[2] - rgb_start[2]) * f)
        row.putpixel((x, 0), (r, g, b))
    return row.resize((w, h))


def _light_tint(rgb, mix=0.92):
    """Mix an accent colour with white. mix=1.0 -> pure white."""
    r = int(rgb[0] + (255 - rgb[0]) * mix)
    g = int(rgb[1] + (255 - rgb[1]) * mix)
    b = int(rgb[2] + (255 - rgb[2]) * mix)
    return (r, g, b)


def _truncate_to_width(draw, text: str, font, max_w: float) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis if lo > 0 else ellipsis


# ---------------------------------------------------------------------------
# Waiting-music bed (broadcast-bright, with numpy) — length = SLIDE_DURATION_S
# ---------------------------------------------------------------------------

def _render_music_wav(out_path: Path) -> None:
    import numpy as np

    sr = 48000
    dur = SLIDE_DURATION_S
    n = int(sr * dur)
    t = np.arange(n) / sr

    bpm = 120
    beat = 60.0 / bpm

    # Chord progression: C - Am - F - G (vi-ish bright pop), 1 measure each
    chords = [
        [261.63, 329.63, 392.00, 523.25],  # C
        [220.00, 261.63, 329.63, 440.00],  # Am
        [174.61, 220.00, 261.63, 349.23],  # F
        [196.00, 246.94, 293.66, 392.00],  # G
    ]
    measure = 4 * beat
    chord_idx = ((t / measure).astype(int)) % 4

    # Pad
    pad = np.zeros(n)
    for ci, freqs in enumerate(chords):
        mask = chord_idx == ci
        layer = np.zeros(n)
        for f in freqs:
            layer += np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * (f + 0.6) * t)
        layer /= 2 * len(freqs)
        pad += mask * layer
    pad *= 0.45

    # Kick on every beat
    beat_phase = np.mod(t, beat)
    kick_env = np.exp(-beat_phase * 24) * (beat_phase < 0.18)
    kick = kick_env * np.sin(2 * np.pi * (60 + 30 * np.exp(-beat_phase * 25)) * beat_phase)

    # Hat on 8ths
    eighth = beat / 2
    e_phase = np.mod(t, eighth)
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n)
    hat = noise * np.exp(-e_phase * 80) * (e_phase < 0.03) * 0.22

    # Arpeggio (cheerful, plays bright 8ths through current chord)
    arp = np.zeros(n)
    eighth_in_meas = (np.floor(t / eighth) % 8).astype(int)
    for ci, freqs in enumerate(chords):
        mask_c = chord_idx == ci
        notes = [freqs[0] * 2, freqs[1] * 2, freqs[2] * 2, freqs[3] * 2,
                 freqs[2] * 2, freqs[1] * 2, freqs[2] * 2, freqs[3] * 2]
        for ei, f in enumerate(notes):
            m = mask_c & (eighth_in_meas == ei)
            env = np.exp(-e_phase * 16) * (e_phase < 0.2)
            arp += m * env * (np.sin(2 * np.pi * f * t) + 0.2 * np.sin(2 * np.pi * f * 2 * t))
    arp *= 0.25

    mix = pad + kick * 0.85 + hat + arp
    peak = float(np.max(np.abs(mix)) or 1.0)
    mix = mix / peak * 0.7
    mix = np.tanh(mix * 0.9) * 1.0

    fi = int(0.5 * sr)
    fo = int(1.5 * sr)
    mix[:fi] *= np.linspace(0, 1, fi)
    mix[-fo:] *= np.linspace(1, 0, fo)

    stereo = np.stack([mix, mix], axis=1)
    i16 = np.clip(stereo * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(i16.tobytes())


# ---------------------------------------------------------------------------
# Encode the final MP4
# ---------------------------------------------------------------------------

def _encode_mp4(png_path: Path, wav_path: Path, out_path: Path) -> None:
    # `-loop 1 -t <SLIDE_DURATION_S>` turns the single PNG into a video. We add a
    # fade-in/out so the slide doesn't pop on/off, and re-encode the
    # audio to AAC. The size baselines around ~3 MB at CRF 22.
    # `scale=...iw*sar` + `setsar=1` flattens any non-square PAR the source
    # PNG might carry (PIL writes square pixels but some loaders
    # interpret the header strictly enough that libx264 complains about
    # odd dimensions otherwise). We also force the dimensions to even
    # values via `scale=trunc(iw/2)*2:trunc(ih/2)*2`.
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(FPS), "-t", str(SLIDE_DURATION_S), "-i", str(png_path),
        "-i", str(wav_path),
        "-vf", (
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,"
            f"fade=t=in:st=0:d=0.6,"
            f"fade=t=out:st={SLIDE_DURATION_S - 0.8}:d=0.8,"
            f"format=yuv420p"
        ),
        "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest",
        # Force mp4 mux explicitly — we write to a `.mp4.partial`
        # temp path (atomic rename into the cache) and ffmpeg can't
        # infer the format from that extension.
        "-f", "mp4",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(
            "whats_next: ffmpeg encode failed (rc=%s)\nCMD: %s\nSTDERR:\n%s",
            result.returncode, " ".join(cmd), result.stderr[-2000:],
        )
        raise RuntimeError(f"ffmpeg failed: rc={result.returncode}")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def render_slide_sync(data: dict) -> Path:
    """Generate the slide (or return the cached path)."""
    # Lazy-create the cache dir here (not at module load) so importing
    # this module never requires write access to /var/lib/meet.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = find_cached(data)
    if cached:
        return cached
    out = cache_path(data)
    with tempfile.TemporaryDirectory(prefix="wnext-") as td:
        td_path = Path(td)
        png = td_path / "slide.png"
        wav = td_path / "music.wav"
        _render_slide_png(data, png)
        _render_music_wav(wav)
        # Encode to a UNIQUE temp file then atomically move into the
        # cache. Two concurrent renders of the same slide (e.g.
        # pre-gen task + on-demand render from `_start_ingress_for_item`)
        # used to share a single `<key>.mp4.partial` path — when one
        # finished and renamed it to `<key>.mp4`, the other ffmpeg's
        # `+faststart` second pass would fail mid-encode with
        # "Unable to re-open … No such file or directory", and the
        # caller fell back to playing the real item without the slide.
        fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, prefix=f"{out.stem}-", suffix=".mp4")
        os.close(fd)
        partial = Path(tmp_path)
        try:
            _encode_mp4(png, wav, partial)
            # Sanity-probe before publishing: an ffmpeg that returns rc=0
            # but writes a corrupt file (e.g. with `mvhd time scale=N/A`)
            # would otherwise enter the cache and silently fail every
            # LiveKit ingress that tries to fetch it (state=3 ERROR), and
            # the caller falls back to direct play — i.e. the slide gets
            # skipped. Cheap insurance: ~50 ms per render.
            if not _validate_mp4(partial):
                raise RuntimeError(f"slide encode produced an unreadable file: {partial}")
            # Atomic; if a peer already wrote `out`, last writer wins —
            # the content is content-addressed by hash so any of the
            # concurrent encodes is equally valid.
            partial.replace(out)
        finally:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
    return out


def _validate_mp4(path: Path) -> bool:
    """ffprobe duration sanity-check. Returns True iff the file reports
    a positive duration — i.e. it's a complete, parseable MP4."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    raw = out.stdout.strip()
    if not raw or raw == "N/A":
        return False
    try:
        return float(raw) > 0
    except ValueError:
        return False


async def render_slide(data: dict) -> Path:
    """Async wrapper that runs the synchronous encode in a thread."""
    return await asyncio.to_thread(render_slide_sync, data)


async def ensure_slide_for_meeting(meeting_id: str) -> Optional[Path]:
    """Build slide data + render it. Returns None when the meeting has
    no eligible upcoming items."""
    with SessionLocal() as db:
        data = build_slide_data(db, meeting_id)
    if data is None:
        return None
    return await render_slide(data)


def schedule_pre_generation(meeting_id: str) -> None:
    """Fire-and-forget background coroutine that pre-warms the slide
    cache for `meeting_id`. Safe to call from sync code (the route
    handlers run inside an asyncio loop)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or not loop.is_running():
        # No running loop (e.g. called from a sync test). Render
        # synchronously in a thread so we still warm the cache, but
        # never block the caller.
        import threading

        def _run():
            try:
                with SessionLocal() as db:
                    data = build_slide_data(db, meeting_id)
                if data is not None:
                    render_slide_sync(data)
            except Exception:
                log.exception("whats_next: pre-gen (sync) failed for %s", meeting_id)

        threading.Thread(target=_run, daemon=True).start()
        return

    async def _coro():
        try:
            await ensure_slide_for_meeting(meeting_id)
        except Exception:
            log.exception("whats_next: pre-gen (async) failed for %s", meeting_id)

    loop.create_task(_coro())


# ---------------------------------------------------------------------------
# TTL cleanup
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS = 7 * 24 * 3600


def evict_stale_slides() -> int:
    """Remove cache entries that haven't been accessed in CACHE_TTL_SECONDS.
    Returns the number of files removed."""
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    import time
    cutoff = time.time() - CACHE_TTL_SECONDS
    for f in CACHE_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_atime < cutoff:
                f.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except Exception:
            log.exception("whats_next: failed to evict %s", f)
    return removed
