#!/usr/bin/env python3
"""Turn an audio-only recording of Ben Conine's TI call into a set of branded MP4s.

Each output part is a 1920x1080, 10 fps H.264 video whose picture is:

  * a blurred + darkened copy of the podcast artwork, scaled to fill the frame
    (the "backdrop"), matching earlier videos like
    ``68_Ben_Conine's_TI_call_June_29th_part4.mp4``;
  * the sharp podcast artwork composited on top, centred horizontally, sized to
    a share of the frame height and offset a share down from the top;
  * a two-line red->orange gradient banner across the bottom, rendered by the
    same code as ``add_banner_to_video.py``;
  * optionally (``--subtitles``), speech-recognised captions burned into the
    empty band between the centre image and the banner.

The soundtrack is the matching slice of the input MP3.

Typical usage
-------------
Split a call recorded on "July 6th" into 45-minute parts, dropping everything
after 3h42m of audio::

    python3 encode_bens_call.py \
        --mp3 "/Volumes/USB10TB2/__video/Ben_Conine_TI_call_06072026.mp3" \
        --date "July 6th" \
        --truncate 3h42m

Split the same call into exactly 7 equal parts instead of 45-minute chunks::

    python3 encode_bens_call.py \
        --mp3 ".../Ben_Conine_TI_call_06072026.mp3" \
        --date "July 6th" \
        --parts 7

Add burned-in captions (needs a whisper CLI, e.g. mlx_whisper in a venv)::

    python3 encode_bens_call.py \
        --mp3 ".../Ben_Conine_TI_call_09072026.mp3" \
        --date "July 9th" \
        --subtitles \
        --whisper-bin /path/to/venv/bin/mlx_whisper

Outputs are written to a fixed folder (``--out-dir``, default
``/Volumes/USB10TB2/__video``) as::

    <NN>_Ben_Conine's_TI_call_<date-slug>_part<k>.mp4

where ``<NN>`` is one greater than the highest leading number already present
on a filename in that folder (so if the highest existing file starts with
``72`` the new files start with ``73``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Reuse the banner renderer from the sibling script so the banner style stays
# identical to every other video in the series. The import is done lazily in
# main() with a clear error message if the file has been moved.
# ---------------------------------------------------------------------------

# Fixed defaults for this recurring job.
DEFAULT_OUT_DIR = Path("/Volumes/USB10TB2/__video")
DEFAULT_ARTWORK = Path(
    "/Volumes/USB10TB2/__video/signal-2026-06-30-20-35-34-247_002.png"
)

# Canvas / render settings, matched to the earlier videos in the series.
CANVAS_W = 1920
CANVAS_H = 1080
FPS = 10

# Centre-image placement (fractions of the canvas).
IMAGE_H_PCT = 0.40   # sharp artwork is 40% of frame height
IMAGE_TOP_PCT = 0.30  # its top edge sits 30% down from the top

# Backdrop look (blurred + darkened fill behind the sharp image).
BACKDROP_BLUR_SIGMA = 40   # gaussian blur strength for the fill
BACKDROP_DARKEN = 0.45     # multiply brightness (0=black, 1=unchanged)

# Default split behaviour when neither --parts nor a custom length is given.
DEFAULT_PART_SECONDS = 45 * 60  # 45 minutes

# --- Subtitles (optional, --subtitles) ---------------------------------------
# Speech-recognised captions are burned into the empty band BETWEEN the centre
# image's bottom edge and the banner's top edge. Style is tuned to stay legible
# over the blurred backdrop and to fit ~2 lines inside that ~108 px gap.
SUB_FONT = "Arial"
SUB_FONTSIZE = 34       # px at 1080p; 2 lines ≈ 82 px, fits the gap
SUB_MAX_CHARS = 50      # greedy word-wrap width, per line (fits most cues in 2)
SUB_MAX_LINES = 2       # lines per on-screen cue (longer cues split by time)
# mlx-whisper on Apple Silicon: fast (GPU via MLX) and, unlike openai-whisper
# with --device mps, does NOT hit the NaN-logits bug. small.en is a good
# quality/speed balance for a spoken conference call.
DEFAULT_WHISPER_MODEL = "mlx-community/whisper-small.en-mlx"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess, raising SystemExit with context on failure."""
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise SystemExit(
            f"error: command failed (rc={result.returncode}): "
            f"{' '.join(cmd[:3])} ...\n{detail}"
        )
    return result


def parse_duration(text: str) -> float:
    """Parse a human duration like '3h42m', '90m', '1h', '45m30s' into seconds.

    Accepts any combination of ``h`` (hours), ``m`` (minutes) and ``s``
    (seconds) tokens, a bare number of seconds, or ``HH:MM:SS`` / ``MM:SS``.
    """
    text = text.strip().lower()
    if not text:
        raise ValueError("empty duration")

    # HH:MM:SS or MM:SS form.
    if ":" in text:
        bits = text.split(":")
        if not all(b.strip() != "" for b in bits) or len(bits) > 3:
            raise ValueError(f"bad clock duration: {text!r}")
        parts = [float(b) for b in bits]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        h, m, s = parts
        return h * 3600 + m * 60 + s

    # Token form: 3h42m, 90m, 45m30s, etc.
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", text)
    if matches:
        # Reject stray leftover characters (e.g. "3h42x").
        consumed = "".join(f"{n}{u}" for n, u in matches).replace(" ", "")
        if consumed != text.replace(" ", ""):
            raise ValueError(f"unrecognised duration: {text!r}")
        unit_secs = {"h": 3600, "m": 60, "s": 1}
        return sum(float(n) * unit_secs[u] for n, u in matches)

    # Bare number = seconds.
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"unrecognised duration: {text!r}") from None


def probe_duration(path: Path) -> float:
    """Return the duration of a media file in seconds via ffprobe."""
    out = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        raise SystemExit(f"error: could not read duration of {path}")


def date_slug(date_text: str) -> str:
    """Turn a free-text date like 'July 6th' into 'July_6th' for filenames."""
    slug = re.sub(r"\s+", "_", date_text.strip())
    # Keep it filename-friendly but readable; drop anything odd.
    slug = re.sub(r"[^\w'-]", "", slug)
    if not slug:
        raise SystemExit("error: --date produced an empty filename slug")
    return slug


def next_prefix(out_dir: Path) -> int:
    """Highest leading number on any filename in out_dir, plus one.

    Falls back to 1 when the folder holds no numbered files.
    """
    highest = 0
    if out_dir.is_dir():
        for entry in out_dir.iterdir():
            m = re.match(r"(\d+)", entry.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def plan_parts(total: float, *, parts: int | None, part_seconds: float) -> list[tuple[float, float]]:
    """Return a list of (start, duration) segments covering ``total`` seconds.

    With ``parts`` set, split into that many equal-length segments.
    Otherwise, split into ``part_seconds``-long chunks with a shorter tail.
    """
    if total <= 0:
        raise SystemExit("error: audio has zero usable duration")

    segments: list[tuple[float, float]] = []
    if parts is not None:
        if parts < 1:
            raise SystemExit("error: --parts must be >= 1")
        seg = total / parts
        for i in range(parts):
            start = i * seg
            # Give the final segment the remainder to avoid rounding drift.
            dur = seg if i < parts - 1 else total - start
            segments.append((start, dur))
    else:
        start = 0.0
        while start < total - 0.001:
            dur = min(part_seconds, total - start)
            segments.append((start, dur))
            start += dur
    return segments


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

def gap_center_y(banner_h_pct: float, bottom_gap_pct: float) -> int:
    """Vertical centre (px) of the empty band between the centre image's
    bottom edge and the banner's top edge — where subtitles are placed."""
    image_bottom = int(round(CANVAS_H * (IMAGE_TOP_PCT + IMAGE_H_PCT)))
    banner_top = (
        CANVAS_H
        - int(round(CANVAS_H * bottom_gap_pct))
        - int(round(CANVAS_H * banner_h_pct))
    )
    return (image_bottom + banner_top) // 2


def extract_audio_slice(mp3: Path, start: float, duration: float, out_wav: Path) -> None:
    """Cut [start, start+duration) from the MP3 to a 16 kHz mono WAV for STT."""
    _run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(mp3),
        "-ar", "16000", "-ac", "1", str(out_wav),
    ])


def transcribe_to_srt(
    wav: Path, whisper_bin: str, model: str, language: str, out_dir: Path
) -> Path:
    """Run an openai-whisper-compatible CLI (default: mlx_whisper) to write an
    SRT alongside the WAV. Returns the SRT path (`<wav_stem>.srt` in out_dir)."""
    _run(
        [
            whisper_bin, str(wav),
            "--model", model,
            "-f", "srt",
            "-o", str(out_dir),
            "--language", language,
            "--verbose", "False",
        ],
        capture=True,  # keep the whisper progress bars out of the job log
    )
    srt = out_dir / f"{wav.stem}.srt"
    if not srt.is_file():
        raise SystemExit(f"error: whisper produced no SRT at {srt}")
    return srt


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    """Parse an SRT file into (start_seconds, end_seconds, text) cues."""
    def ts(t: str) -> float:
        h, m, s = t.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    cues: list[tuple[float, float, str]] = []
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.search(
            r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)", lines[1]
        )
        if not m:
            continue
        body = " ".join(lines[2:]).strip()
        if body:
            cues.append((ts(m.group(1)), ts(m.group(2)), body))
    return cues


def _wrap_cue(text: str, max_chars: int, max_lines: int) -> list[list[str]]:
    """Greedy word-wrap `text` into lines of <= max_chars, then group those
    lines into on-screen chunks of <= max_lines lines each."""
    lines: list[str] = []
    cur = ""
    for w in text.split():
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]


def _ass_time(sec: float) -> str:
    """Seconds -> ASS timestamp `H:MM:SS.cc` (centiseconds)."""
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape_text(s: str) -> str:
    """Escape characters that libass would treat as override syntax."""
    return s.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def write_ass(cues: list[tuple[float, float, str]], out_ass: Path, center_y: int) -> None:
    """Render cues to an ASS file whose every line is \\pos-anchored to the gap
    centre, so the captions sit between the image and the banner. Long cues are
    split into multiple <= SUB_MAX_LINES-line chunks with their time span
    apportioned by character count."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {CANVAS_W}\n"
        f"PlayResY: {CANVAS_H}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # White fill, black outline (3 px) + soft shadow → legible over the
        # blurred backdrop. Alignment 5 (middle-centre); \pos overrides per line.
        f"Style: Sub,{SUB_FONT},{SUB_FONTSIZE},&H00FFFFFF,&H000000FF,"
        "&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,5,40,40,40,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    parts = [header]
    x = CANVAS_W // 2
    for start, end, text in cues:
        chunks = _wrap_cue(text, SUB_MAX_CHARS, SUB_MAX_LINES)
        if not chunks:
            continue
        weights = [sum(len(ln) for ln in ch) for ch in chunks]
        total_w = sum(weights) or 1
        span = max(0.0, end - start)
        t0 = start
        for ch, w in zip(chunks, weights):
            t1 = t0 + span * (w / total_w)
            body = r"\N".join(_ass_escape_text(ln) for ln in ch)
            parts.append(
                f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Sub,,0,0,0,,"
                f"{{\\an5\\pos({x},{center_y})}}{body}\n"
            )
            t0 = t1
    out_ass.write_text("".join(parts), encoding="utf-8")


def _ass_filter_arg(p: Path) -> str:
    """Escape an ASS path for use inside an ffmpeg filtergraph (single-quoted)."""
    return str(p).replace("\\", "\\\\").replace("'", r"\'")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def build_base_frame(artwork: Path, out_png: Path) -> None:
    """Compose the still frame: blurred/darkened backdrop + sharp centred image.

    The banner is added later by add_banner_to_video, so this frame has none.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    art = Image.open(artwork).convert("RGBA")

    # --- Backdrop: cover-fill the canvas, then blur and darken. ---
    aw, ah = art.size
    scale = max(CANVAS_W / aw, CANVAS_H / ah)
    bw, bh = int(round(aw * scale)), int(round(ah * scale))
    backdrop = art.convert("RGB").resize((bw, bh), Image.LANCZOS)
    # Centre-crop to the canvas.
    left = (bw - CANVAS_W) // 2
    top = (bh - CANVAS_H) // 2
    backdrop = backdrop.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(BACKDROP_BLUR_SIGMA))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(BACKDROP_DARKEN)

    canvas = backdrop.convert("RGBA")

    # --- Sharp centre image: 40% of height, centred X, 30% down from top. ---
    target_h = int(round(CANVAS_H * IMAGE_H_PCT))
    fg_scale = target_h / ah
    target_w = int(round(aw * fg_scale))
    fg = art.resize((target_w, target_h), Image.LANCZOS)
    fx = (CANVAS_W - target_w) // 2
    fy = int(round(CANVAS_H * IMAGE_TOP_PCT))
    canvas.alpha_composite(fg, (fx, fy))

    canvas.convert("RGB").save(out_png)


def render_part(
    base_png: Path,
    mp3: Path,
    start: float,
    duration: float,
    banner_png: Path,
    out_path: Path,
    ass_path: Path | None = None,
) -> None:
    """Encode one part: still frame + banner overlay + the audio slice, and —
    when `ass_path` is given — the subtitles burned into the gap after the
    banner (so a 2-line caption sits above the banner, below the image)."""
    # Chain: image ← banner overlay ← [subtitles] ← pixel format.
    chain = "[0:v][2:v]overlay=0:0:format=auto"
    if ass_path is not None:
        chain += f",ass='{_ass_filter_arg(ass_path)}'"
    chain += ",format=yuv420p[v]"
    cmd = [
        "ffmpeg", "-y",
        # Looped still image as the video source.
        "-loop", "1", "-framerate", str(FPS), "-i", str(base_png),
        # The audio slice.
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(mp3),
        # Banner overlay (single frame, reused every frame).
        "-i", str(banner_png),
        "-filter_complex", chain,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-r", str(FPS),
        "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "16000",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    _run(cmd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mp3", required=True, help="Input MP3 recording")
    p.add_argument(
        "--date", required=True,
        help="Human date of the call, e.g. \"July 6th\". Used in the banner "
             "text and in the output filenames.",
    )
    p.add_argument(
        "--truncate", default=None,
        help="Drop audio past this length, e.g. '3h42m', '90m', '1:30:00'. "
             "Default: use the whole file.",
    )
    p.add_argument(
        "--parts", type=int, default=None,
        help="Split into exactly this many equal parts. Default: split into "
             f"{DEFAULT_PART_SECONDS // 60}-minute parts (shorter last part).",
    )
    p.add_argument(
        "--part-length", default=None,
        help="Override the per-part length for chunk mode, e.g. '30m'. "
             "Ignored when --parts is given.",
    )
    p.add_argument(
        "--artwork", default=str(DEFAULT_ARTWORK),
        help="Podcast artwork PNG used for the centre image and the backdrop.",
    )
    p.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR),
        help="Folder the MP4 parts are written to (fixed for this job).",
    )
    p.add_argument(
        "--line2", default="more info on: benjaminconine.substack.com",
        help="Second (smaller) banner line, constant across parts.",
    )
    p.add_argument(
        "--subtitles", action="store_true",
        help="Speech-recognise each part and burn captions into the gap "
             "between the centre image and the banner. Requires --whisper-bin.",
    )
    p.add_argument(
        "--whisper-bin", default=None,
        help="Path to an openai-whisper-compatible CLI (e.g. a venv's "
             "mlx_whisper). Required with --subtitles.",
    )
    p.add_argument(
        "--whisper-model", default=DEFAULT_WHISPER_MODEL,
        help=f"STT model. Default: {DEFAULT_WHISPER_MODEL}",
    )
    p.add_argument(
        "--whisper-language", default="en",
        help="Spoken language of the recording (default: en).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan (segments + output names) and exit.",
    )
    args = p.parse_args()

    # --- Validate inputs. ---
    mp3 = Path(args.mp3).expanduser()
    if not mp3.is_file():
        print(f"error: MP3 not found: {mp3}", file=sys.stderr)
        return 1

    artwork = Path(args.artwork).expanduser()
    if not artwork.is_file():
        print(f"error: artwork not found: {artwork}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).expanduser()
    if not args.dry_run:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"error: cannot create out-dir {out_dir}: {e}", file=sys.stderr)
            return 1

    # Pull in the banner renderer + its geometry from the sibling script, so
    # the subtitle band is computed from the same banner size the video uses.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from add_banner_to_video import (
            _render_banner_png,
            BANNER_H_PCT,
            BOTTOM_GAP_PCT,
        )
    except ImportError as e:
        print(f"error: cannot import add_banner_to_video.py: {e}", file=sys.stderr)
        return 1

    # Validate the subtitle toolchain up front (before any long encode).
    if args.subtitles:
        if not args.whisper_bin:
            print("error: --subtitles requires --whisper-bin", file=sys.stderr)
            return 1
        if not Path(args.whisper_bin).expanduser().exists():
            print(f"error: whisper bin not found: {args.whisper_bin}", file=sys.stderr)
            return 1

    # --- Work out the timeline. ---
    total = probe_duration(mp3)
    if args.truncate:
        try:
            limit = parse_duration(args.truncate)
        except ValueError as e:
            print(f"error: bad --truncate value: {e}", file=sys.stderr)
            return 1
        if limit <= 0:
            print("error: --truncate must be positive", file=sys.stderr)
            return 1
        total = min(total, limit)

    part_seconds = DEFAULT_PART_SECONDS
    if args.part_length:
        try:
            part_seconds = parse_duration(args.part_length)
        except ValueError as e:
            print(f"error: bad --part-length value: {e}", file=sys.stderr)
            return 1
        if part_seconds <= 0:
            print("error: --part-length must be positive", file=sys.stderr)
            return 1

    segments = plan_parts(total, parts=args.parts, part_seconds=part_seconds)
    slug = date_slug(args.date)
    prefix = next_prefix(out_dir)

    # --- Show the plan. ---
    def fmt(sec: float) -> str:
        m, s = divmod(int(round(sec)), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"

    print(f"input   : {mp3}")
    print(f"artwork : {artwork}")
    print(f"out-dir : {out_dir}")
    print(f"total   : {fmt(total)}  ({len(segments)} parts, prefix {prefix:02d})")
    outputs = []
    for i, (start, dur) in enumerate(segments, start=1):
        name = f"{prefix:02d}_Ben_Conine's_TI_call_{slug}_part{i}.mp4"
        outputs.append(out_dir / name)
        print(f"  part {i}: {fmt(start)} +{fmt(dur)}  -> {name}")

    if args.dry_run:
        return 0

    # Subtitle band centre (px) — computed from the actual banner geometry.
    sub_center_y = gap_center_y(BANNER_H_PCT, BOTTOM_GAP_PCT) if args.subtitles else 0

    # --- Build the shared still frame once, then encode each part. ---
    with tempfile.TemporaryDirectory(prefix="bens-call-") as td:
        base_png = Path(td) / "base.png"
        build_base_frame(artwork, base_png)

        for i, ((start, dur), out_path) in enumerate(zip(segments, outputs), start=1):
            line1 = f"TI-TV Ben Conine's TI call from {args.date} part {i}"
            banner_png = Path(td) / f"banner_{i}.png"
            _render_banner_png(CANVAS_W, CANVAS_H, line1, args.line2, banner_png)

            ass_path: Path | None = None
            if args.subtitles:
                print(f"\n[{i}/{len(segments)}] transcribing {out_path.name} ...")
                wav = Path(td) / f"part_{i}.wav"
                extract_audio_slice(mp3, start, dur, wav)
                srt = transcribe_to_srt(
                    wav, args.whisper_bin, args.whisper_model,
                    args.whisper_language, Path(td),
                )
                cues = parse_srt(srt)
                ass_path = Path(td) / f"part_{i}.ass"
                write_ass(cues, ass_path, sub_center_y)
                print(f"  {len(cues)} caption cues")
                wav.unlink(missing_ok=True)  # reclaim space before the encode

            print(f"[{i}/{len(segments)}] encoding {out_path.name} ...")
            render_part(base_png, mp3, start, dur, banner_png, out_path, ass_path)
            print(f"  wrote {out_path}")

    print(f"\nDone. {len(segments)} parts written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
