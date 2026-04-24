import os
import tempfile
from pathlib import Path

import pytest

# Configure env before app imports so settings pick these up.
_tmp = Path(tempfile.mkdtemp(prefix="meet-api-test-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp / 'test.db'}")
os.environ.setdefault("RECORDINGS_DIR", str(_tmp / "recordings"))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-never-used-in-prod")
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
