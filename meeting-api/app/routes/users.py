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
    anonymise_email_in_join_log: bool = False
    dont_log_my_ip: bool = False


class PreferencesUpdate(BaseModel):
    language: str | None = Field(default=None, min_length=2, max_length=10)
    anonymise_email_in_join_log: bool | None = None
    dont_log_my_ip: bool | None = None


def _out(row: UserPreferences | None) -> PreferencesOut:
    if not row:
        return PreferencesOut(language=None, language_set_manually=False)
    return PreferencesOut(
        language=row.language,
        language_set_manually=row.language_set_manually,
        anonymise_email_in_join_log=bool(row.anonymise_email_in_join_log),
        dont_log_my_ip=bool(row.dont_log_my_ip),
    )


@router.get("/me/preferences")
def get_my_preferences(user: RequireUser, db: Session = Depends(get_db)) -> PreferencesOut:
    return _out(db.get(UserPreferences, user.sub))


@router.put("/me/preferences")
def update_my_preferences(
    body: PreferencesUpdate,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> PreferencesOut:
    row = db.get(UserPreferences, user.sub)
    if not row:
        row = UserPreferences(user_id=user.sub)
        db.add(row)

    touched = False
    if body.language is not None:
        row.language = body.language
        row.language_set_manually = True
        touched = True
    if body.anonymise_email_in_join_log is not None:
        row.anonymise_email_in_join_log = body.anonymise_email_in_join_log
        touched = True
    if body.dont_log_my_ip is not None:
        row.dont_log_my_ip = body.dont_log_my_ip
        touched = True
    if touched:
        row.updated_at = utcnow()

    db.commit()
    db.refresh(row)
    return _out(row)
