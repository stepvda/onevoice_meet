from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.db import get_db
from app.models import UserPreferences, utcnow

router = APIRouter(prefix="/v1")


class PreferencesOut(BaseModel):
    language: str | None
    language_set_manually: bool


class PreferencesUpdate(BaseModel):
    language: str | None = Field(default=None, min_length=2, max_length=10)


@router.get("/me/preferences")
def get_my_preferences(user: RequireUser, db: Session = Depends(get_db)) -> PreferencesOut:
    row = db.get(UserPreferences, user.user_id)
    if not row:
        return PreferencesOut(language=None, language_set_manually=False)
    return PreferencesOut(language=row.language, language_set_manually=row.language_set_manually)


@router.put("/me/preferences")
def update_my_preferences(
    body: PreferencesUpdate,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> PreferencesOut:
    row = db.get(UserPreferences, user.user_id)
    if not row:
        row = UserPreferences(user_id=user.user_id)
        db.add(row)

    if body.language is not None:
        row.language = body.language
        row.language_set_manually = True
        row.updated_at = utcnow()

    db.commit()
    db.refresh(row)
    return PreferencesOut(language=row.language, language_set_manually=row.language_set_manually)
