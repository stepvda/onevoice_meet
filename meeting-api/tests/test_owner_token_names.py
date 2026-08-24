"""Regression tests for participant display names on the moderator token
mint (POST /v1/meetings/{id}/token).

Bug history: the mint only used `body.display_name` to refresh the meeting's
`owner_name` snapshot — for cohosts it was dropped entirely, and SSO user
rows never learn a name (auto-provisioned shells, PATCH /v1/me blocked), so
SSO cohosts always rendered as "User <sub>" in the room, and SSO owners did
too whenever the browser→one.witysk.org name fetch failed.
"""
import os

import pytest
from jose import jwt


def _sso_token(sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "type": "access"},
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )


def _auth(sub: str) -> dict:
    return {"Authorization": f"Bearer {_sso_token(sub)}"}


def _livekit_name(livekit_token: str) -> str | None:
    return jwt.get_unverified_claims(livekit_token).get("name")


@pytest.fixture
def meeting(client) -> dict:
    # display_name deliberately omitted: simulates a creation where the SPA's
    # one.witysk.org name fetch failed, so no owner_name snapshot exists.
    r = client.post(
        "/api/v1/meetings",
        json={"display_title": "Name test"},
        headers=_auth("42"),
    )
    assert r.status_code == 201, r.text
    return r.json()["meeting"]


def test_owner_mint_uses_body_display_name(client, meeting):
    r = client.post(
        f"/api/v1/meetings/{meeting['id']}/token",
        json={"display_name": "Stephane V"},
        headers=_auth("42"),
    )
    assert r.status_code == 200, r.text
    assert _livekit_name(r.json()["token"]) == "Stephane V"


def test_owner_remint_survives_failed_name_fetch(client, meeting):
    # First mint carries the name; the SSO shell row snapshots it.
    client.post(
        f"/api/v1/meetings/{meeting['id']}/token",
        json={"display_name": "Stephane V"},
        headers=_auth("42"),
    )
    # Second mint with a null name (fetch failed) must NOT degrade to the
    # "User 42" placeholder.
    r = client.post(
        f"/api/v1/meetings/{meeting['id']}/token",
        json={"display_name": None},
        headers=_auth("42"),
    )
    assert r.status_code == 200, r.text
    assert _livekit_name(r.json()["token"]) == "Stephane V"


def test_cohost_mint_uses_body_display_name(client, meeting):
    # Provision the cohost user, then promote them.
    r = client.post(
        f"/api/v1/meetings/{meeting['id']}/cohosts",
        json={"user_sub": "77"},
        headers=_auth("42"),
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/meetings/{meeting['id']}/token",
        json={"display_name": "Cora Host"},
        headers=_auth("77"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "cohost"
    assert _livekit_name(r.json()["token"]) == "Cora Host"


def test_placeholder_still_used_when_no_name_known(client):
    # Fresh sub: the shared test DB persists User rows across tests, and any
    # earlier named mint by "42" would (correctly) be snapshotted on its row.
    r = client.post(
        "/api/v1/meetings",
        json={"display_title": "Nameless"},
        headers=_auth("99"),
    )
    assert r.status_code == 201, r.text
    meeting = r.json()["meeting"]
    r = client.post(
        f"/api/v1/meetings/{meeting['id']}/token",
        json={"display_name": "   "},
        headers=_auth("99"),
    )
    assert r.status_code == 200, r.text
    # Whitespace-only names are treated as absent; with no snapshot anywhere
    # the safe placeholder (never the email) is used.
    assert _livekit_name(r.json()["token"]) == "User 99"
