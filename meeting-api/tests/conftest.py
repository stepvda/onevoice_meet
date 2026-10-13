import os
import tempfile
from pathlib import Path

import pytest

# Configure env before app imports so settings pick these up.
_tmp = Path(tempfile.mkdtemp(prefix="meet-api-test-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp / 'test.db'}")
os.environ.setdefault("RECORDINGS_DIR", str(_tmp / "recordings"))
os.environ.setdefault("ON_DEMAND_PREVIEW_DIR", str(_tmp / "previews"))
os.environ.setdefault("FRONTEND_DIR", str(_tmp / "frontend"))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-never-used-in-prod")
os.environ.setdefault("LOG_DIR", str(_tmp / "logs"))
os.environ.setdefault("LIVEKIT_API_KEY", "APItest")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret-64-chars-placeholder-test-secret-64-chars-placeholder")
os.environ.setdefault("LIVEKIT_WEBHOOK_KEY", "APItest")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("TURN_STATIC_AUTH_SECRET", "")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def access_token() -> str:
    from jose import jwt

    return jwt.encode(
        {"sub": "42", "email": "user@example.com", "type": "access"},
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )


MAIL_ISSUER = "https://mail.example.test"


class MailIssuer:
    """A stand-in One Voice Mail: an EC P-256 signing key published through a
    patched JWKS fetch (counted, so tests can see the cache at work)."""

    def __init__(self) -> None:
        self.keys: list[dict] = []
        self.fetches = 0
        self.kid, self.pem = self.new_key()

    def new_key(self) -> tuple[str, str]:
        import uuid

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from jose import jwk

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        kid = uuid.uuid4().hex
        public = jwk.construct(pem, "ES256").public_key().to_dict()
        self.keys.append({**public, "kid": kid, "use": "sig"})
        return kid, pem

    def rotate(self) -> None:
        """Mail signs with a new key from now on (published alongside the old one)."""
        self.kid, self.pem = self.new_key()

    def fetch(self) -> list[dict]:
        self.fetches += 1
        return list(self.keys)

    def token(self, sub: str = "6f1c1d7e-0000-4000-8000-000000000001", *, kid: str | None = None, pem: str | None = None,
              **claims) -> str:
        import time

        from jose import jwt

        now = int(time.time())
        body = {
            "iss": MAIL_ISSUER, "aud": "meet", "sub": sub, "type": "access", "email": f"{sub[:8]}@mail.example.test",
            "name": "Mail User", "iat": now, "exp": now + 900, "jti": f"j-{now}",
        }
        body.update(claims)
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, pem or self.pem, algorithm="ES256", headers={"kid": kid or self.kid, "typ": "JWT"})

    def headers(self, sub: str = "6f1c1d7e-0000-4000-8000-000000000001", **claims) -> dict:
        return {"Authorization": f"Bearer {self.token(sub, **claims)}"}


@pytest.fixture
def mail(monkeypatch):
    """Mail integration on (MAIL_ISSUER set) with a fresh signing key."""
    from app import auth
    from app.config import settings

    issuer = MailIssuer()
    monkeypatch.setattr(settings, "mail_issuer", MAIL_ISSUER)
    monkeypatch.setattr(settings, "mail_jwks_url", "")
    monkeypatch.setattr(settings, "mail_audience", "meet")
    monkeypatch.setattr(auth, "_fetch_mail_jwks", issuer.fetch)
    monkeypatch.setitem(auth._MAIL_JWKS_CACHE, "keys", None)
    monkeypatch.setitem(auth._MAIL_JWKS_CACHE, "at", 0.0)
    monkeypatch.setitem(auth._MAIL_JWKS_CACHE, "forced_at", 0.0)
    return issuer
