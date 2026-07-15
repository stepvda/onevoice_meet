#!/usr/bin/env python3
"""Add the standard red->orange banner PLUS burned-in speech-recognised
subtitles to an existing interview/podcast video.

Unlike `encode_bens_call.py` (which builds video from a still image + raw
audio), this script takes an ALREADY-EXISTING video file, upscales it to
1920x1080 (matching the rest of the series), overlays the two-line banner
from `add_banner_to_video.py`, and burns in captions positioned just above
the banner's top edge.

Usage:
    python3 add_banner_and_subtitles.py \
        --input  /path/to/in.mp4 \
        --output /path/to/out.mp4 \
        --line1  "TI-TV David Atkins interviews James Martinez" \
        --line2  "youtube channel: www.youtube.com/@TIDavidA" \
        --whisper-bin /path/to/venv/bin/mlx_whisper
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Output canvas — matches the rest of the video series.
CANVAS_W = 1920
CANVAS_H = 1080

# Subtitle style. Anchored just above the banner's top edge (computed from
# add_banner_to_video's own geometry so it always matches the banner size).
SUB_FONT = "Arial"
SUB_FONTSIZE = 34
SUB_MAX_CHARS = 50
SUB_MAX_LINES = 2
SUB_GAP_ABOVE_BANNER = 16  # px between the caption's baseline block and the banner

DEFAULT_WHISPER_MODEL = "mlx-community/whisper-small.en-mlx"


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
            f"{' '.join(cmd[:4])} ...\n{detail}"
        )
    return result


def probe_duration(path: Path) -> float:
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


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

def extract_audio(input_video: Path, out_wav: Path) -> None:
    """Extract the full soundtrack to a 16 kHz mono WAV for STT."""
    _run([
        "ffmpeg", "-y", "-i", str(input_video),
        "-vn", "-ar", "16000", "-ac", "1", str(out_wav),
    ])


def transcribe_to_srt(
    wav: Path, whisper_bin: str, model: str, language: str, out_dir: Path
) -> Path:
    _run(
        [
            whisper_bin, str(wav),
            "--model", model,
            "-f", "srt",
            "-o", str(out_dir),
            "--language", language,
            "--verbose", "False",
        ],
        capture=True,
    )
    srt = out_dir / f"{wav.stem}.srt"
    if not srt.is_file():
        raise SystemExit(f"error: whisper produced no SRT at {srt}")
    return srt


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
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
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape_text(s: str) -> str:
    return s.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def write_ass(
    cues: list[tuple[float, float, str]], out_ass: Path, bottom_y: int
) -> None:
    """Render cues to an ASS file. Each cue is anchored (an2 = bottom-centre)
    with its baseline at `bottom_y`, i.e. just above the banner. Long cues
    split into <= SUB_MAX_LINES-line chunks with their time span apportioned
    by character count, same approach as encode_bens_call.py."""
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
        f"Style: Sub,{SUB_FONT},{SUB_FONTSIZE},&H00FFFFFF,&H000000FF,"
        "&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,40,1\n\n"
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
                f"{{\\an2\\pos({x},{bottom_y})}}{body}\n"
            )
            t0 = t1
    out_ass.write_text("".join(parts), encoding="utf-8")


def _ass_filter_arg(p: Path) -> str:
    return str(p).replace("\\", "\\\\").replace("'", r"\'")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(
    input_video: Path,
    banner_png: Path,
    ass_path: Path | None,
    out_path: Path,
) -> None:
    """Upscale the source video to the canvas, overlay the banner, and
    (optionally) burn in the subtitles."""
    chain = (
        f"[0:v]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black[base];"
        "[base][1:v]overlay=0:0:format=auto"
    )
    if ass_path is not None:
        chain += f",ass='{_ass_filter_arg(ass_path)}'"
    chain += ",format=yuv420p[v]"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_video),
        "-i", str(banner_png),
        "-filter_complex", chain,
        "-map", "[v]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(cmd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--input", required=True, help="Existing input video file")
    p.add_argument("--output", required=True, help="Output video file")
    p.add_argument("--line1", required=True, help="First (top) banner line")
    p.add_argument("--line2", required=True, help="Second (smaller) banner line")
    p.add_argument(
        "--subtitles", action="store_true", default=True,
        help="Speech-recognise the video and burn captions just above the "
             "banner (default: on). Pass --no-subtitles to skip.",
    )
    p.add_argument("--no-subtitles", dest="subtitles", action="store_false")
    p.add_argument(
        "--whisper-bin", default=None,
        help="Path to an openai-whisper-compatible CLI (e.g. a venv's "
             "mlx_whisper). Required unless --no-subtitles.",
    )
    p.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    p.add_argument("--whisper-language", default="en")
    args = p.parse_args()

    in_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()
    if not in_path.exists():
        print(f"error: input not found: {in_path}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.subtitles and not args.whisper_bin:
        print("error: --subtitles requires --whisper-bin (or pass --no-subtitles)", file=sys.stderr)
        return 1
    if args.subtitles and not Path(args.whisper_bin).expanduser().exists():
        print(f"error: whisper bin not found: {args.whisper_bin}", file=sys.stderr)
        return 1

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

    # Banner geometry on the OUTPUT canvas (1920x1080), matching
    # add_banner_to_video's own math so the burned banner lines up exactly
    # with where the subtitle band is anchored.
    banner_h = max(1, int(round(CANVAS_H * BANNER_H_PCT)))
    bottom_gap = int(round(CANVAS_H * BOTTOM_GAP_PCT))
    banner_top_y = CANVAS_H - bottom_gap - banner_h
    sub_bottom_y = banner_top_y - SUB_GAP_ABOVE_BANNER

    duration = probe_duration(in_path)
    print(f"input    : {in_path}")
    print(f"output   : {out_path}")
    print(f"duration : {duration:.1f}s")
    print(f"banner   : top_y={banner_top_y}  subtitles anchored at y={sub_bottom_y}")

    with tempfile.TemporaryDirectory(prefix="banner-sub-") as td:
        tdp = Path(td)

        banner_png = tdp / "banner.png"
        _render_banner_png(CANVAS_W, CANVAS_H, args.line1, args.line2, banner_png)

        ass_path: Path | None = None
        if args.subtitles:
            print("\ntranscribing audio ...")
            wav = tdp / "audio.wav"
            extract_audio(in_path, wav)
            srt = transcribe_to_srt(
                wav, args.whisper_bin, args.whisper_model, args.whisper_language, tdp
            )
            cues = parse_srt(srt)
            ass_path = tdp / "subs.ass"
            write_ass(cues, ass_path, sub_bottom_y)
            print(f"  {len(cues)} caption cues")
            wav.unlink(missing_ok=True)

        print("\nencoding ...")
        render(in_path, banner_png, ass_path, out_path)

    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
