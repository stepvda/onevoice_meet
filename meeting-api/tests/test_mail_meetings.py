"""Meetings API additions used by One Voice Mail (docs/MAIL_INTEGRATION.md §Meetings)."""
import uuid

import pytest
import redis

from app.config import settings


@pytest.fixture(autouse=True)
def _fresh_join_rate_limit():
    """These tests join a lot; start each from an empty per-IP budget."""
    from app.routes import tokens

    try:
        tokens._redis.delete("anon_token:testclient")
    except redis.RedisError:
        pass


def _create(client, headers, **body) -> dict:
    r = client.post("/api/v1/meetings", headers=headers, json={"display_title": "Board meeting", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_with_integration(client, mail):
    h = mail.headers(str(uuid.uuid4()))
    out = _create(
        client, h,
        scheduled_at="2026-10-20T17:00:00Z", ends_at="2026-10-20T19:00:00Z", duration_minutes=120,
        password="4711", recurrence_rule="FREQ=WEEKLY", allow_anonymous=True,
        integration={"source": "mail", "ref": "meeting:abc"}, display_name="Ann Example",
    )
    m = out["meeting"]
    assert out["join_url"] == m["join_url"] == f"{settings.public_url}/{m['room_name']}"
    assert m["allow_anonymous"] is True and m["has_password"] is True and m["require_password"] is True
    assert m["external_source"] == "mail" and m["external_ref"] == "meeting:abc"
    assert m["recurrence_rule"] == "FREQ=WEEKLY" and m["duration_minutes"] == 120
    assert m["owner_name"] == "Ann Example"
    # Not listed anywhere although guests may join.
    assert m["list_for_anonymous"] is False and m["list_for_authenticated"] is False
    got = client.get(f"/api/v1/meetings/{m['id']}", headers=h).json()
    assert got["join_url"] == m["join_url"] and got["external_ref"] == "meeting:abc"


def test_long_events_and_names_are_accepted(client, mail):
    """Mail sends any calendar event's length and its user's name or e-mail."""
    h = mail.headers(str(uuid.uuid4()))
    m = _create(client, h, duration_minutes=1440, display_name="x" * 200)["meeting"]
    assert m["duration_minutes"] == 480 and m["owner_name"] == "x" * 120
    assert _create(client, h, duration_minutes=2)["meeting"]["duration_minutes"] == 5
    out = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={"duration_minutes": 600}).json()
    assert out["duration_minutes"] == 480


def test_defaults_without_integration(client, access_token):
    m = _create(client, {"Authorization": f"Bearer {access_token}"})["meeting"]
    assert m["allow_anonymous"] is False and m["has_password"] is False
    assert m["external_source"] is None and m["external_ref"] is None


def test_invalid_integration_source_is_refused(client, access_token):
    r = client.post(
        "/api/v1/meetings", headers={"Authorization": f"Bearer {access_token}"},
        json={"display_title": "X", "integration": {"source": "Mail Server!", "ref": "x"}},
    )
    assert r.status_code == 422


def test_patch_schedule_password_and_access(client, mail):
    h = mail.headers(str(uuid.uuid4()))
    m = _create(client, h, password="old", allow_anonymous=True)["meeting"]
    room = m["room_name"]

    r = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={
        "display_title": "Renamed", "scheduled_at": "2026-11-01T09:00:00Z", "ends_at": "2026-11-01T10:30:00Z",
        "duration_minutes": 90, "recurrence_rule": "FREQ=MONTHLY", "password": "new",
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["display_title"] == "Renamed" and out["duration_minutes"] == 90 and out["recurrence_rule"] == "FREQ=MONTHLY"
    assert out["scheduled_at"].startswith("2026-11-01T09:00") and out["has_password"] is True
    assert client.post(f"/api/v1/rooms/{room}/anon-token", json={"display_name": "G", "password": "old"}).status_code == 401
    assert client.post(f"/api/v1/rooms/{room}/anon-token", json={"display_name": "G", "password": "new"}).status_code == 200

    # Only presets; null clears; fields left out are unchanged.
    out = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={"recurrence_rule": "FREQ=YEARLY", "ends_at": None}).json()
    assert out["recurrence_rule"] is None and out["ends_at"] is None and out["duration_minutes"] == 90
    assert out["scheduled_at"].startswith("2026-11-01T09:00")

    # "" (or null) removes the password.
    out = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={"password": ""}).json()
    assert out["has_password"] is False and out["require_password"] is False
    assert client.post(f"/api/v1/rooms/{room}/anon-token", json={"display_name": "G"}).status_code == 200
    out = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={"password": "again"}).json()
    assert out["has_password"] is True
    out = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={"password": None}).json()
    assert out["has_password"] is False

    out = client.patch(f"/api/v1/meetings/{m['id']}", headers=h, json={"allow_anonymous": False}).json()
    assert out["allow_anonymous"] is False
    r = client.post(f"/api/v1/rooms/{room}/anon-token", json={"display_name": "G"})
    assert r.status_code == 403


def test_patch_is_for_moderators_only(client, mail):
    m = _create(client, mail.headers(str(uuid.uuid4())))["meeting"]
    r = client.patch(f"/api/v1/meetings/{m['id']}", headers=mail.headers(str(uuid.uuid4())), json={"password": ""})
    assert r.status_code == 404


def test_anonymous_join_with_allow_anonymous(client, mail):
    h = mail.headers(str(uuid.uuid4()))
    private = _create(client, h)["meeting"]
    assert client.post(f"/api/v1/rooms/{private['room_name']}/anon-token", json={"display_name": "G"}).status_code == 403
    guests = _create(client, h, allow_anonymous=True, password="pw")["meeting"]
    room = guests["room_name"]
    assert client.post(f"/api/v1/rooms/{room}/anon-token", json={"display_name": "G"}).status_code == 401
    r = client.post(f"/api/v1/rooms/{room}/anon-token", json={"display_name": "G", "password": "pw"})
    assert r.status_code == 200, r.text
    assert r.json()["room_name"] == room
    # Still not discoverable.
    assert room not in {x["room_name"] for x in client.get("/api/v1/public-meetings").json()}


def test_delete_ends_the_meeting(client, mail, monkeypatch):
    from app.routes import meetings as meetings_mod

    class _Room:
        async def delete_room(self, _req):
            return None

    class _LK:
        room = _Room()

        async def aclose(self):
            return None

    monkeypatch.setattr(meetings_mod, "livekit_api", lambda: _LK())
    h = mail.headers(str(uuid.uuid4()))
    m = _create(client, h)["meeting"]
    assert client.delete(f"/api/v1/meetings/{m['id']}", headers=h).status_code == 204
    assert client.get(f"/api/v1/meetings/{m['id']}", headers=h).json()["is_active"] is False


def test_embed_config(client, monkeypatch):
    monkeypatch.setattr(settings, "embed_parent_origins", "https://mail.witysk.org, http://localhost:5180/ ,")
    r = client.get("/api/v1/embed/config")
    assert r.status_code == 200
    assert r.json() == {"parent_origins": ["https://mail.witysk.org", "http://localhost:5180"]}
    monkeypatch.setattr(settings, "embed_parent_origins", "")
    assert client.get("/api/v1/embed/config").json() == {"parent_origins": []}
