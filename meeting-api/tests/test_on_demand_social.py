"""On Demand social-card endpoints + meta injection helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routes.on_demand_social import (
    _inject_meta,
    fmt_duration,
    iso_duration,
)

FFMPEG = shutil.which("ffmpeg")


def _make_meeting_and_item(tmp_path, public_enabled: bool = True) -> tuple[str, str]:
    from app.db import SessionLocal
    from app.models import Meeting, PlaybackItem

    src = tmp_path / f"video-{os.urandom(4).hex()}.mp4"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-t", "20", str(src),
        ],
        check=True,
    )
    mid = "M" + os.urandom(6).hex()
    iid = "I" + os.urandom(6).hex()
    slug = "slug-" + os.urandom(4).hex()
    with SessionLocal() as db:
        db.add(
            Meeting(
                id=mid,
                room_name=f"room-{mid}",
                display_title="Test Show",
                owner_user_id="42",
                owner_name="Alice",
                is_active=True,
                hidden=False,
                public_enabled=public_enabled,
                public_slug=slug,
            )
        )
        db.add(
            PlaybackItem(
                id=iid,
                meeting_id=mid,
                position=0,
                filename="01_Intro_Talk.mp4",
                file_path=str(src),
                file_size_bytes=src.stat().st_size,
                mime_type="video/mp4",
                duration_seconds=600.0,
            )
        )
        db.commit()
    return mid, iid


@pytest.fixture
def on_demand_video(client, tmp_path):
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    mid, iid = _make_meeting_and_item(tmp_path)
    return {"meeting_id": mid, "id": iid}


def test_duration_helpers():
    assert fmt_duration(305) == "5:05"
    assert fmt_duration(3725) == "1:02:05"
    assert iso_duration(305) == "PT5M5S"
    assert iso_duration(3725) == "PT1H2M5S"
    assert iso_duration(0) == "PT0S"


def test_inject_meta_replaces_generic_tags():
    shell = (
        "<html><head><title>Generic</title>"
        '<meta property="og:title" content="gen" />'
        '<meta name="description" content="gen" />'
        '<link rel="canonical" href="https://meet.witysk.org/" />'
        "</head><body><div id=\"root\"></div></body></html>"
    )
    out = _inject_meta(
        shell,
        "<title>Custom</title>"
        '<meta property="og:title" content="Custom &amp; Co" />',
    )
    assert "<title>Custom</title>" in out
    assert "Generic" not in out
    assert 'content="gen"' not in out
    assert out.count("og:title") == 1
    assert out.index("og:title") < out.index("</head>")
    assert '<div id="root"></div>' in out


def test_generic_page(client):
    r = client.get("/api/v1/on-demand/page")
    assert r.status_code == 200
    assert 'property="og:type" content="website"' in r.text
    assert "og-image.jpg" in r.text


def test_page_unknown_video_falls_back_to_generic(client):
    r = client.get("/api/v1/on-demand/page?v=does-not-exist")
    assert r.status_code == 200
    assert 'property="og:type" content="website"' in r.text


def test_share_page_injects_video_meta(client, on_demand_video):
    vid = on_demand_video["id"]
    r = client.get(f"/api/v1/on-demand/page?v={vid}")
    assert r.status_code == 200
    assert 'property="og:type" content="video.other"' in r.text
    assert f"v={vid}" in r.text
    assert "poster.jpg" in r.text
    assert "preview.mp4" in r.text
    assert "Intro Talk" in r.text  # cleaned filename
    assert "application/ld+json" in r.text
    assert "twitter:player" in r.text


def test_share_page_uses_mounted_spa_shell(client, on_demand_video, tmp_path, monkeypatch):
    """When the built SPA shell is mounted, its <head> is rewritten in place
    while the body (and therefore the app bootstrap) is preserved."""
    from app.config import settings

    shell_dir = tmp_path / "frontend"
    shell_dir.mkdir()
    (shell_dir / "index.html").write_text(
        "<html><head><title>Generic</title>"
        '<meta property="og:title" content="gen" />'
        "</head><body><div id=\"root\"></div>"
        '<script type="module" src="/assets/index-abc.js"></script></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "frontend_dir", str(shell_dir))
    r = client.get(f"/api/v1/on-demand/page?v={on_demand_video['id']}")
    assert r.status_code == 200
    assert '<div id="root"></div>' in r.text
    assert "/assets/index-abc.js" in r.text
    assert "Generic" not in r.text
    assert 'property="og:type" content="video.other"' in r.text


def test_poster_endpoint(client, on_demand_video):
    vid = on_demand_video["id"]
    r = client.get(f"/api/v1/on-demand/items/{vid}/poster.jpg")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert len(r.content) > 2000
    # The composed card must not be a black frame (the production bug this
    # fixes) and must be the advertised 1200x630.
    from io import BytesIO

    from PIL import Image, ImageStat

    img = Image.open(BytesIO(r.content))
    assert img.size == (1200, 630)
    assert ImageStat.Stat(img.convert("L")).mean[0] > 20
    # Cached second call serves the same bytes.
    r2 = client.get(f"/api/v1/on-demand/items/{vid}/poster.jpg")
    assert r2.content == r.content


def test_frame_score_prefers_scenes_over_slides():
    """The card image should be a scene, not a black frame or a text slide
    (a bright fair-use/title card produced ugly overlapped cards)."""
    import random
    from io import BytesIO

    from PIL import Image, ImageDraw

    from app.services import previews

    def to_jpeg(img) -> bytes:
        buf = BytesIO()
        img.save(buf, "JPEG")
        return buf.getvalue()

    random.seed(7)
    scene = Image.new("RGB", (192, 108))
    px = scene.load()
    for x in range(192):
        for y in range(108):
            px[x, y] = (
                random.randint(40, 220),
                random.randint(40, 220),
                random.randint(40, 220),
            )
    slide = Image.new("RGB", (192, 108), (4, 4, 6))
    d = ImageDraw.Draw(slide)
    for i in range(6):
        d.rectangle([10, 8 + i * 14, 180, 16 + i * 14], fill=(240, 240, 240))
    black = Image.new("RGB", (192, 108), (2, 2, 2))

    scene_score = previews._frame_score(to_jpeg(scene))
    assert scene_score > previews._frame_score(to_jpeg(slide))
    assert scene_score > previews._frame_score(to_jpeg(black))


def test_pick_frame_time_skips_black_intro(tmp_path):
    """A video with a black opening must not produce a black card: the
    chosen frame moves past the fade-in."""
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    src = tmp_path / "black-intro.mp4"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=7",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:d=15",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt", "yuv420p", "-t", "20", str(src),
        ],
        check=True,
    )

    from app.services import previews

    chosen = previews.pick_frame_time(src, 20.0)
    assert chosen >= 8.0, chosen


def test_clip_endpoint(client, on_demand_video):
    vid = on_demand_video["id"]
    r = client.get(f"/api/v1/on-demand/items/{vid}/preview.mp4")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert len(r.content) > 1000


def test_embed_and_oembed(client, on_demand_video):
    vid = on_demand_video["id"]
    r = client.get(f"/api/v1/on-demand/embed?v={vid}")
    assert r.status_code == 200
    assert "<video" in r.text and "poster.jpg" in r.text

    assert client.get("/api/v1/on-demand/embed?v=nope").status_code == 404

    r3 = client.get(
        "/api/v1/on-demand/oembed",
        params={"url": f"https://meet.witysk.org/on-demand?v={vid}"},
    )
    assert r3.status_code == 200
    body = r3.json()
    assert body["type"] == "video"
    assert "poster.jpg" in body["thumbnail_url"]
    assert "<iframe" in body["html"]


def test_head_requests_supported(client, on_demand_video):
    """Link-preview crawlers commonly probe with HEAD first."""
    vid = on_demand_video["id"]
    for path, status in [
        ("/api/v1/on-demand/page", 200),
        (f"/api/v1/on-demand/page?v={vid}", 200),
        (f"/api/v1/on-demand/items/{vid}/poster.jpg", 200),
        (f"/api/v1/on-demand/items/{vid}/preview.mp4", 200),
        ("/api/v1/on-demand/embed?v=nope", 404),
        (f"/api/v1/on-demand/oembed?url=https%3A%2F%2Fmeet.witysk.org%2Fon-demand%3Fv%3D{vid}", 200),
    ]:
        r = client.head(path)
        assert r.status_code == status, (path, r.status_code)
        assert r.content == b"", path


def test_head_media_does_not_generate(client, on_demand_video):
    """Crawler HEAD probes must never spawn ffmpeg; the artifact is only
    produced by the follow-up GET."""
    from app.config import settings

    vid = on_demand_video["id"]
    preview_dir = Path(settings.on_demand_preview_dir)

    r = client.head(f"/api/v1/on-demand/items/{vid}/poster.jpg")
    assert r.status_code == 200, r.text
    assert r.content == b""
    assert r.headers["content-type"] == "image/jpeg"
    assert not list(preview_dir.glob(f"{vid}-poster-*"))

    r2 = client.get(f"/api/v1/on-demand/items/{vid}/poster.jpg")
    assert r2.status_code == 200
    assert list(preview_dir.glob(f"{vid}-poster-*"))


def test_broken_file_serves_placeholder_and_backs_off(client, tmp_path):
    """A corrupt source must not re-spawn ffmpeg on every request: the GIF
    returns 503 both times (the second from the failure cooldown), and the
    poster degrades to the Pillow placeholder instead of erroring."""
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    from app.db import SessionLocal
    from app.models import PlaybackItem

    _mid, iid = _make_meeting_and_item(tmp_path)
    with SessionLocal() as db:
        item = db.get(PlaybackItem, iid)
        Path(item.file_path).write_bytes(b"definitely not a video")
    g1 = client.get(f"/api/v1/on-demand/items/{iid}/preview.gif")
    g2 = client.get(f"/api/v1/on-demand/items/{iid}/preview.gif")
    assert g1.status_code == 503, g1.text
    assert g2.status_code == 503, g2.text

    p = client.get(f"/api/v1/on-demand/items/{iid}/poster.jpg")
    assert p.status_code == 200, p.text
    assert p.headers["content-type"] == "image/jpeg"
    assert len(p.content) > 1000


def test_ineligible_video_returns_404(client, tmp_path):
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    mid, iid = _make_meeting_and_item(tmp_path, public_enabled=False)
    r = client.get(f"/api/v1/on-demand/items/{iid}/poster.jpg")
    assert r.status_code == 404
    assert client.get(f"/api/v1/on-demand/embed?v={iid}").status_code == 404
