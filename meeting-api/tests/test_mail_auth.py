"""Tokens minted by One Voice Mail (docs/MAIL_INTEGRATION.md §Auth)."""
import os
import time
import uuid

from jose import jwt

from app import auth
from app.config import settings
from app.db import SessionLocal
from app.models import User

from .conftest import MAIL_ISSUER


def _sub() -> str:
    return str(uuid.uuid4())


def _sso(sub: str, **claims) -> dict:
    token = jwt.encode({"sub": sub, "type": "access", **claims}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def test_mail_user_is_provisioned_with_mail_identity(client, mail):
    sub = _sub()
    r = client.get("/api/v1/me", headers=mail.headers(sub, email=f"Ann.{sub[:6]}@Example.org", name="Ann Example"))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["kind"] == "mail"
    assert me["external_id"] == f"mail:{sub}"
    assert me["email"] == f"ann.{sub[:6]}@example.org"
    assert me["name"] == "Ann Example"
    # Mail users create meetings like SSO users (no trial, no billing).
    assert me["is_admin"] is True and me["is_platform_admin"] is False

    created = client.post("/api/v1/meetings", headers=mail.headers(sub), json={"display_title": "From Mail"})
    assert created.status_code == 201, created.text
    assert created.json()["meeting"]["owner_user_id"] == f"mail:{sub}"


def test_name_and_email_follow_the_token(client, mail):
    sub = _sub()
    client.get("/api/v1/me", headers=mail.headers(sub, email=f"{sub[:8]}@one.example", name="First"))
    me = client.get("/api/v1/me", headers=mail.headers(sub, email=f"{sub[:8]}@two.example", name="Second")).json()
    assert me["email"] == f"{sub[:8]}@two.example" and me["name"] == "Second"


def test_email_taken_by_another_account_is_not_synced(client, mail):
    email = f"taken-{uuid.uuid4().hex[:8]}@example.org"
    db = SessionLocal()
    try:
        db.add(User(kind="native", email=email, password_hash="x"))
        db.commit()
    finally:
        db.close()
    sub = _sub()
    me = client.get("/api/v1/me", headers=mail.headers(sub, email=email, name="Copy Cat")).json()
    assert me["kind"] == "mail" and me["email"] is None and me["name"] == "Copy Cat"
    # …and an existing Mail user does not take it over either.
    other = _sub()
    client.get("/api/v1/me", headers=mail.headers(other, email=f"{other[:8]}@free.example"))
    me = client.get("/api/v1/me", headers=mail.headers(other, email=email)).json()
    assert me["email"] == f"{other[:8]}@free.example"


def test_onevoice_sub_maps_to_the_sso_account(client, mail):
    onevoice = str(900000 + int(time.time() * 1000) % 99999)
    sub = _sub()
    r = client.get("/api/v1/me", headers=mail.headers(sub, onevoice_sub=onevoice, name="Linked Person"))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["kind"] == "sso" and me["external_id"] == onevoice and me["name"] == "Linked Person"
    created = client.post("/api/v1/meetings", headers=mail.headers(sub, onevoice_sub=onevoice), json={"display_title": "Linked"}).json()
    assert created["meeting"]["owner_user_id"] == onevoice
    # The same Meet account at meet.witysk.org (one.witysk.org SSO token).
    mine = client.get("/api/v1/meetings", headers=_sso(onevoice)).json()
    assert created["meeting"]["id"] in {m["id"] for m in mine}


def test_wrong_audience_is_refused(client, mail):
    assert client.get("/api/v1/me", headers=mail.headers(_sub(), aud="mail")).status_code == 401


def test_expired_token_is_refused(client, mail):
    past = int(time.time()) - 3600
    assert client.get("/api/v1/me", headers=mail.headers(_sub(), iat=past - 900, exp=past)).status_code == 401


def test_token_without_exp_is_refused(client, mail):
    assert client.get("/api/v1/me", headers=mail.headers(_sub(), exp=None)).status_code == 401


def test_only_access_tokens(client, mail):
    assert client.get("/api/v1/me", headers=mail.headers(_sub(), type="refresh")).status_code == 401


def test_unknown_kid_refetches_once(client, mail):
    sub = _sub()
    assert client.get("/api/v1/me", headers=mail.headers(sub)).status_code == 200
    assert mail.fetches == 1
    assert client.get("/api/v1/me", headers=mail.headers(sub)).status_code == 200
    assert mail.fetches == 1  # cached
    mail.rotate()
    assert client.get("/api/v1/me", headers=mail.headers(sub)).status_code == 200
    assert mail.fetches == 2  # the new kid made it refetch
    # A kid Mail never published: one more refetch at most, then 401; right
    # after a refetch another unknown kid does not hammer Mail.
    kid, pem = "not-published", mail.pem
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {mail.token(sub, kid=kid, pem=pem)}"}).status_code == 401
    assert mail.fetches == 2


def test_cache_expires_after_ten_minutes(client, mail, monkeypatch):
    sub = _sub()
    assert client.get("/api/v1/me", headers=mail.headers(sub)).status_code == 200
    monkeypatch.setitem(auth._MAIL_JWKS_CACHE, "at", time.time() - 601)
    assert client.get("/api/v1/me", headers=mail.headers(sub)).status_code == 200
    assert mail.fetches == 2


def test_signature_from_another_key_is_refused(client, mail):
    _kid, other_pem = mail.new_key()
    mail.keys.pop()  # never published
    token = mail.token(_sub(), kid=mail.kid, pem=other_pem)
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_hs256_token_claiming_the_mail_issuer_is_refused(client, mail):
    # The shared HS256 secret must not be able to speak for Mail.
    assert client.get("/api/v1/me", headers=_sso(_sub(), iss=MAIL_ISSUER, aud="meet", exp=int(time.time()) + 600)).status_code == 401


def test_other_issuers_unchanged(client, mail, monkeypatch):
    monkeypatch.setattr(auth, "_onevoice_jwks", lambda: [])
    me = client.get("/api/v1/me", headers=_sso("4242", email="sso4242@example.com")).json()
    assert me["kind"] == "sso" and me["external_id"] == "4242"
    # A Mail-signed token with another issuer goes to one.witysk.org's keys.
    assert client.get("/api/v1/me", headers=mail.headers(_sub(), iss="https://elsewhere.example")).status_code == 401
    assert mail.fetches == 0


def test_integration_off_refuses_mail_tokens(client, mail, monkeypatch):
    monkeypatch.setattr(settings, "mail_issuer", "")
    monkeypatch.setattr(auth, "_onevoice_jwks", lambda: [])
    assert client.get("/api/v1/me", headers=mail.headers(_sub())).status_code == 401
    assert mail.fetches == 0


def test_mail_users_have_no_billing_vouchers_refresh_or_profile_edits(client, mail):
    h = mail.headers(_sub())
    assert client.post("/api/v1/billing/orders", headers=h, json={"kind": "monthly"}).status_code == 403
    assert client.get("/api/v1/billing/me/billing-history", headers=h).json() == []
    assert client.post("/api/v1/vouchers/redeem", headers=h, json={"code": "ABCD-EFGH"}).status_code == 403
    assert client.post("/api/v1/auth/refresh", headers=h).status_code == 403
    assert client.patch("/api/v1/me", headers=h, json={"name": "Other"}).status_code == 403
