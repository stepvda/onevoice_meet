def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_me_requires_auth(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 401


def test_me_with_token(client, access_token):
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {access_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "42"
    assert body["email"] == "user@example.com"


def test_create_meeting(client, access_token):
    r = client.post(
        "/api/v1/meetings",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"display_title": "Test meeting"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["meeting"]["display_title"] == "Test meeting"
    assert body["meeting"]["owner_user_id"] == "42"
    assert body["join_url"].endswith(body["meeting"]["room_name"])


def test_anon_token_rejects_bad_password(client, access_token):
    created = client.post(
        "/api/v1/meetings",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"display_title": "Protected", "password": "hunter2"},
    ).json()["meeting"]

    r = client.post(
        f"/api/v1/rooms/{created['room_name']}/anon-token",
        json={"display_name": "Alice", "password": "wrong"},
    )
    assert r.status_code == 401


def test_anon_token_happy_path(client, access_token):
    created = client.post(
        "/api/v1/meetings",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"display_title": "Open"},
    ).json()["meeting"]

    r = client.post(
        f"/api/v1/rooms/{created['room_name']}/anon-token",
        json={"display_name": "Bob"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room_name"] == created["room_name"]
    assert body["token"]
    assert body["livekit_url"].startswith("wss://")
