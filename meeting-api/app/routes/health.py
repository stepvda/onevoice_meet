from fastapi import APIRouter

from app.auth import RequireUser

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/v1/me")
def whoami(user: RequireUser) -> dict:
    return {"user_id": user.user_id, "email": user.email}
