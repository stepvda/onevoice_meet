"""
Platform admin panel — user management, IP blocking, IDS.

All endpoints under /v1/admin require `is_platform_admin=True`. The bootstrap
list (`PLATFORM_ADMIN_EMAILS` in `.env`) seeds the first admins on startup.
After that, admins can promote / demote each other from the panel.

User actions this exposes:
  - list / search / view a user
  - toggle is_platform_admin
  - suspend / unsuspend (sets is_disabled — blocks all future requests)
  - reset native password (admin sets a new one)
  - delete (hard delete, native users only)

IP actions:
  - list / add / remove / toggle blocked IPs (exact, CIDR, dash range)

IDS:
  - list active temp blocks; manually unblock an IP
  - read recent in-memory events
  - read persisted security_events for history
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from passlib.hash import argon2
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import RequirePlatformAdmin
from app.db import get_db
from app.models import BlockedIP, SecurityEvent, User
from app.services import ip_block
from app.services.intrusion_detector import detector

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ─── User management ───────────────────────────────────────────────────


class AdminUserOut(BaseModel):
    id: int
    kind: str
    external_id: str | None
    email: str | None
    username: str | None
    name: str | None
    is_admin: bool
    is_platform_admin: bool
    is_disabled: bool
    disable_reason: str | None
    trial_used: bool
    entitlement_kind: str | None
    entitlement_expires_at: datetime | None
    totp_enabled: bool
    email_otp_enabled: bool
    created_at: datetime
    updated_at: datetime


class AdminUpdateUserBody(BaseModel):
    is_platform_admin: bool | None = None
    is_disabled: bool | None = None
    disable_reason: str | None = Field(default=None, max_length=255)


class AdminSetPasswordBody(BaseModel):
    new_password: str = Field(min_length=8, max_length=200)


def _to_admin_user(u: User) -> AdminUserOut:
    now = datetime.now(timezone.utc)
    return AdminUserOut(
        id=u.id,
        kind=u.kind,
        external_id=u.external_id,
        email=u.email,
        username=u.username,
        name=u.name,
        is_admin=u.is_admin_now(now),
        is_platform_admin=bool(u.is_platform_admin),
        is_disabled=bool(u.is_disabled),
        disable_reason=u.disable_reason,
        trial_used=bool(u.trial_used),
        entitlement_kind=u.entitlement_kind,
        entitlement_expires_at=u.entitlement_expires_at,
        totp_enabled=bool(u.totp_enabled),
        email_otp_enabled=bool(u.email_otp_enabled),
        created_at=u.created_at,
        updated_at=u.updated_at,
    )


@router.get("/users")
def list_users(
    _admin: RequirePlatformAdmin,
    q: str | None = Query(default=None, max_length=120, description="Filter by email/username/name (substring)"),
    kind: str | None = Query(default=None, pattern="^(sso|native)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(User)
    if kind:
        query = query.filter(User.kind == kind)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                User.email.ilike(like),
                User.username.ilike(like),
                User.name.ilike(like),
            )
        )
    total = query.count()
    rows = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "users": [_to_admin_user(u) for u in rows]}


@router.get("/users/{user_id}")
def get_user(user_id: int, _admin: RequirePlatformAdmin, db: Session = Depends(get_db)) -> AdminUserOut:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    return _to_admin_user(u)


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    body: AdminUpdateUserBody,
    admin: RequirePlatformAdmin,
    db: Session = Depends(get_db),
) -> AdminUserOut:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    # Guardrail: an admin can't demote or disable themselves. Avoids the
    # operator accidentally locking themselves out of the panel.
    if u.id == admin.user_id:
        if body.is_platform_admin is False:
            raise HTTPException(status_code=400, detail="cannot revoke your own platform-admin rights")
        if body.is_disabled is True:
            raise HTTPException(status_code=400, detail="cannot disable your own account")
    if body.is_platform_admin is not None:
        u.is_platform_admin = body.is_platform_admin
    if body.is_disabled is not None:
        u.is_disabled = body.is_disabled
        if body.is_disabled:
            u.disable_reason = (body.disable_reason or "").strip() or u.disable_reason
        else:
            u.disable_reason = None
    elif body.disable_reason is not None:
        # Allow updating the reason text without flipping the flag.
        u.disable_reason = body.disable_reason.strip() or None
    db.commit()
    db.refresh(u)
    return _to_admin_user(u)


@router.post("/users/{user_id}/set-password")
def admin_set_password(
    user_id: int,
    body: AdminSetPasswordBody,
    _admin: RequirePlatformAdmin,
    db: Session = Depends(get_db),
) -> dict:
    """Force a new password on a native user. SSO users have no local
    password and are managed on one.witysk.org — refuse for them."""
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    if u.kind != "native":
        raise HTTPException(status_code=400, detail="only native users have a local password")
    u.password_hash = argon2.hash(body.new_password)
    db.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: RequirePlatformAdmin, db: Session = Depends(get_db)) -> dict:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    if u.id == admin.user_id:
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    if u.kind != "native":
        raise HTTPException(status_code=400, detail="SSO users are managed on one.witysk.org")
    db.delete(u)
    db.commit()
    return {"ok": True}


# ─── Blocked IPs ───────────────────────────────────────────────────────


class BlockedIPOut(BaseModel):
    id: int
    ip_address: str
    reason: str | None
    blocked_by_user_id: int | None
    block_count: int
    is_enabled: bool
    created_at: datetime
    live_hits: int  # in-memory hits since last persistence flush


class CreateBlockedIPBody(BaseModel):
    ip_address: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=255)


class UpdateBlockedIPBody(BaseModel):
    is_enabled: bool | None = None
    reason: str | None = Field(default=None, max_length=255)


@router.get("/blocked-ips")
def list_blocked_ips(_admin: RequirePlatformAdmin, db: Session = Depends(get_db)) -> list[BlockedIPOut]:
    rows = db.query(BlockedIP).order_by(BlockedIP.created_at.desc()).all()
    live = ip_block.hit_counts()
    return [
        BlockedIPOut(
            id=r.id,
            ip_address=r.ip_address,
            reason=r.reason,
            blocked_by_user_id=r.blocked_by_user_id,
            block_count=r.block_count or 0,
            is_enabled=bool(r.is_enabled),
            created_at=r.created_at,
            live_hits=live.get(r.ip_address, 0),
        )
        for r in rows
    ]


@router.post("/blocked-ips", status_code=201)
def add_blocked_ip(
    body: CreateBlockedIPBody,
    admin: RequirePlatformAdmin,
    db: Session = Depends(get_db),
) -> BlockedIPOut:
    # Validate format up front so we don't store unparseable rows.
    try:
        ip_block.parse_entry(body.ip_address)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    raw = body.ip_address.strip()
    existing = db.query(BlockedIP).filter_by(ip_address=raw).first()
    if existing:
        raise HTTPException(status_code=409, detail="that IP / range is already on the list")
    row = BlockedIP(
        ip_address=raw,
        reason=(body.reason or "").strip() or None,
        blocked_by_user_id=admin.user_id,
        is_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    ip_block.reload()
    return BlockedIPOut(
        id=row.id,
        ip_address=row.ip_address,
        reason=row.reason,
        blocked_by_user_id=row.blocked_by_user_id,
        block_count=0,
        is_enabled=True,
        created_at=row.created_at,
        live_hits=0,
    )


@router.patch("/blocked-ips/{ip_id}")
def update_blocked_ip(
    ip_id: int,
    body: UpdateBlockedIPBody,
    _admin: RequirePlatformAdmin,
    db: Session = Depends(get_db),
) -> BlockedIPOut:
    row = db.get(BlockedIP, ip_id)
    if not row:
        raise HTTPException(status_code=404, detail="entry not found")
    if body.is_enabled is not None:
        row.is_enabled = body.is_enabled
    if body.reason is not None:
        row.reason = body.reason.strip() or None
    db.commit()
    db.refresh(row)
    ip_block.reload()
    return BlockedIPOut(
        id=row.id,
        ip_address=row.ip_address,
        reason=row.reason,
        blocked_by_user_id=row.blocked_by_user_id,
        block_count=row.block_count or 0,
        is_enabled=bool(row.is_enabled),
        created_at=row.created_at,
        live_hits=ip_block.hit_counts().get(row.ip_address, 0),
    )


@router.delete("/blocked-ips/{ip_id}")
def delete_blocked_ip(ip_id: int, _admin: RequirePlatformAdmin, db: Session = Depends(get_db)) -> dict:
    row = db.get(BlockedIP, ip_id)
    if not row:
        raise HTTPException(status_code=404, detail="entry not found")
    db.delete(row)
    db.commit()
    ip_block.reload()
    return {"ok": True}


# ─── IDS ───────────────────────────────────────────────────────────────


class IdsStatusOut(BaseModel):
    enabled: bool
    tracked_ips: int
    temp_blocked: int
    events_in_memory: int
    temp_blocks: list[dict]


@router.get("/ids/status")
def ids_status(_admin: RequirePlatformAdmin) -> IdsStatusOut:
    s = detector.stats()
    return IdsStatusOut(
        enabled=s["enabled"],
        tracked_ips=s["tracked_ips"],
        temp_blocked=s["temp_blocked"],
        events_in_memory=s["events_in_memory"],
        temp_blocks=detector.temp_blocks(),
    )


@router.get("/ids/events")
def ids_events(
    _admin: RequirePlatformAdmin,
    limit: int = Query(default=100, ge=1, le=500),
    persisted: bool = Query(default=False, description="Read from security_events table instead of in-memory ring"),
    db: Session = Depends(get_db),
) -> list[dict]:
    if persisted:
        rows = db.query(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(limit).all()
        return [
            {
                "ts": r.created_at.isoformat() if r.created_at else None,
                "event_type": r.event_type,
                "severity": r.severity,
                "ip": r.ip_address,
                "user_id": r.user_id,
                "handle": r.handle,
                "path": r.path,
                "user_agent": r.user_agent,
                "details": r.details,
            }
            for r in rows
        ]
    return detector.recent_events(limit=limit)


@router.post("/ids/unblock/{ip}")
def ids_unblock(ip: str, _admin: RequirePlatformAdmin) -> dict:
    removed = detector.unblock(ip)
    return {"ok": True, "was_blocked": removed}
