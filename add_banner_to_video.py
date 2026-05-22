#!/usr/bin/env python3
"""Overlay a two-line banner across an entire video.

The banner sits at the bottom of the frame, sized as a percentage of the
video height, and uses the same red->orange gradient + white-hairline
style as the "What's up next" auto-generated slide.

Usage:
    python3 add_banner_to_video.py \
        --input  /path/to/in.mp4 \
        --output /path/to/out.mp4 \
        --line1  "TI-TV - David Atkins interviews TI Dan Williams" \
        --line2  "youtube channel: www.youtube.com/@TIDavidA"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Banner geometry (fractions of the video height).
BANNER_H_PCT = 0.15   # banner is 15% of video height
BOTTOM_GAP_PCT = 0.05  # banner sits this far above the bottom edge

# Gradient colors, matched to whats_next_slide.py
RGB_START = (230, 57, 70)   # #E63946
RGB_END = (247, 127, 0)     # #F77F00

_FONT_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_REG_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _first_existing(paths: list[str]) -> str:
    for p in paths:
        if os.path.exists(p):
            return p
    return ""


def _probe_video(path: Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(path),
    ])
    info = json.loads(out)
    s = info["streams"][0]
    return int(s["width"]), int(s["height"])


def _horizontal_gradient(size, rgb_start, rgb_end):
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


def _fit_font(draw, text: str, font_path: str, max_w: int, max_h: int, start_size: int):
    """Largest font that fits text inside (max_w, max_h)."""
    from PIL import ImageFont
    size = start_size
    while size > 6:
        f = ImageFont.truetype(font_path, size)
        w = draw.textlength(text, font=f)
        ascent, descent = f.getmetrics()
        h = ascent + descent
        if w <= max_w and h <= max_h:
            return f, w, h
        size -= 1
    f = ImageFont.truetype(font_path, 6)
    return f, draw.textlength(text, font=f), sum(f.getmetrics())


def _render_banner_png(vid_w: int, vid_h: int, line1: str, line2: str, out_png: Path) -> None:
    from PIL import Image, ImageDraw

    banner_h = max(1, int(round(vid_h * BANNER_H_PCT)))
    bottom_gap = int(round(vid_h * BOTTOM_GAP_PCT))
    banner_y = vid_h - bottom_gap - banner_h

    canvas = Image.new("RGBA", (vid_w, vid_h), (0, 0, 0, 0))
    grad = _horizontal_gradient((vid_w, banner_h), RGB_START, RGB_END).convert("RGBA")
    canvas.paste(grad, (0, banner_y))

    draw = ImageDraw.Draw(canvas, "RGBA")

    # White hairline + dark shadow line at the top edge.
    hairline = max(1, vid_h // 360)
    draw.rectangle([0, banner_y, vid_w, banner_y + hairline], fill=(255, 255, 255, 230))
    draw.rectangle([0, banner_y + hairline, vid_w, banner_y + 2 * hairline], fill=(0, 0, 0, 60))

    bold = _first_existing(_FONT_BOLD_CANDIDATES)
    reg = _first_existing(_FONT_REG_CANDIDATES)
    if not bold or not reg:
        raise SystemExit("Required fonts (Arial / DejaVu) not found")

    side_pad = max(8, vid_w // 40)
    inner_w = vid_w - 2 * side_pad

    top_pad = max(2, banner_h // 14)
    bot_pad = max(2, banner_h // 14)
    gap = max(1, banner_h // 28)
    avail_h = banner_h - top_pad - bot_pad - gap
    h1_budget = int(avail_h * 0.58)
    h2_budget = avail_h - h1_budget

    f1, w1, h1 = _fit_font(draw, line1, bold, inner_w, h1_budget, banner_h)
    f2, w2, h2 = _fit_font(draw, line2, reg, inner_w, h2_budget, banner_h)

    block_h = h1 + gap + h2
    block_y0 = banner_y + (banner_h - block_h) // 2
    x1 = (vid_w - int(w1)) // 2
    x2 = (vid_w - int(w2)) // 2
    y2 = block_y0 + h1 + gap

    shadow = (0, 0, 0, 110)
    draw.text((x1 + 1, block_y0 + 1), line1, fill=shadow, font=f1)
    draw.text((x1, block_y0), line1, fill=(255, 255, 255, 255), font=f1)
    draw.text((x2 + 1, y2 + 1), line2, fill=shadow, font=f2)
    draw.text((x2, y2), line2, fill=(255, 255, 255, 240), font=f2)

    canvas.save(out_png)


def _encode(input_video: Path, banner_png: Path, output_video: Path) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_video),
        "-i", str(banner_png),
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "copy",
        str(output_video),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg failed (rc={result.returncode})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="Input video file")
    p.add_argument("--output", required=True, help="Output video file")
    p.add_argument("--line1", required=True, help="First (top) banner line")
    p.add_argument("--line2", required=True, help="Second (smaller) banner line")
    args = p.parse_args()

    in_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()
    if not in_path.exists():
        print(f"error: input not found: {in_path}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vid_w, vid_h = _probe_video(in_path)
    print(f"input: {vid_w}x{vid_h}")

    with tempfile.TemporaryDirectory(prefix="banner-") as td:
        banner_png = Path(td) / "banner.png"
        _render_banner_png(vid_w, vid_h, args.line1, args.line2, banner_png)
        _encode(in_path, banner_png, out_path)

    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
