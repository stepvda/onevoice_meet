"""
PayPal billing — Phase 3.

Two payment paths, both via the user's browser (frontend renders PayPal's
JS Buttons SDK; the user approves on PayPal; meet's backend creates and
captures via the REST API).

  Monthly subscription (€2/month, recurring):
    1. Frontend renders the Subscribe button with PAYPAL_PLAN_ID_MONTHLY.
    2. User approves; PayPal calls our /v1/billing/subscriptions/activated
       with the new subscription_id.
    3. We GET the subscription, store the row, grant the entitlement.
    4. Webhook events afterwards keep status / next_billing_at in sync.

  Annual one-shot (€20):
    1. Frontend renders the Pay button.
    2. createOrder callback hits POST /v1/billing/orders → returns the
       PayPal order_id.
    3. User approves on PayPal; onApprove callback hits POST
       /v1/billing/orders/{id}/capture, which captures via PayPal AND
       grants a 12-month entitlement.

Webhook (POST /v1/billing/webhook) handles ongoing events: payment
failed, subscription cancelled, dispute opened, etc. Each maps to an
entitlement update on the matched user row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.models import PaypalOrder, PaypalSubscription, User, Voucher
from app.services.paypal import (
    PaypalApiError,
    PaypalNotConfigured,
    capture_order,
    cancel_subscription,
    create_order,
    get_subscription,
    verify_webhook,
)

router = APIRouter(prefix="/v1/billing")


class BillingConfigOut(BaseModel):
    """Public config — what the SPA needs to render PayPal buttons. The
    `enabled` flag tells the UI whether to show the buttons or a
    "PayPal not configured" notice."""
    enabled: bool
    client_id: str
    plan_id_monthly: str
    plan_id_annual: str
    monthly_price: str
    monthly_currency: str
    annual_price: str
    annual_currency: str


@router.get("/config")
def get_config() -> BillingConfigOut:
    return BillingConfigOut(
        enabled=bool(settings.paypal_client_id),
        client_id=settings.paypal_client_id,
        plan_id_monthly=settings.paypal_plan_id_monthly,
        plan_id_annual=settings.paypal_plan_id_annual,
        monthly_price=settings.paypal_monthly_price,
        monthly_currency=settings.paypal_monthly_currency,
        annual_price=settings.paypal_annual_price,
        annual_currency=settings.paypal_annual_currency,
    )


# ─── One-shot order (monthly bill-once OR annual bill-once) ───────────


class CreateOrderBody(BaseModel):
    # "monthly" → 30 days @ paypal_monthly_price
    # "annual"  → 365 days @ paypal_annual_price
    kind: str = "annual"


class CreateOrderOut(BaseModel):
    order_id: str
    kind: str


@router.post("/orders", status_code=201)
async def create_billing_order(
    user: RequireUser,
    body: CreateOrderBody | None = None,
    db: Session = Depends(get_db),
) -> CreateOrderOut:
    if user.kind == "sso":
        raise HTTPException(status_code=403, detail="SSO accounts already have admin rights")
    kind = (body.kind if body else "annual").lower()
    if kind not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="kind must be 'monthly' or 'annual'")
    if kind == "monthly":
        amount = settings.paypal_monthly_price
        currency = settings.paypal_monthly_currency
    else:
        amount = settings.paypal_annual_price
        currency = settings.paypal_annual_currency
    try:
        order = await create_order(
            amount=amount,
            currency=currency,
            return_url=f"{settings.public_url}/upgrade",
            cancel_url=f"{settings.public_url}/upgrade",
        )
    except PaypalNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except PaypalApiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    row = PaypalOrder(
        user_id=user.user_id,
        paypal_order_id=order["id"],
        amount=amount,
        currency=currency,
        status=order.get("status", "CREATED"),
        kind=kind,
    )
    db.add(row)
    db.commit()
    return CreateOrderOut(order_id=order["id"], kind=kind)


@router.post("/orders/{order_id}/capture")
async def capture_billing_order(
    order_id: str, user: RequireUser, db: Session = Depends(get_db)
) -> dict:
    """Captures the approved order AND grants entitlement: 30 days for
    a monthly bill-once, 365 for an annual bill-once. Idempotent on the
    meet side: if the order row already shows COMPLETED, returns the
    existing state without re-granting (so a double-click in the UI
    doesn't double-extend the user's expiry)."""
    row = (
        db.query(PaypalOrder)
        .filter_by(paypal_order_id=order_id, user_id=user.user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="order not found")
    if row.status == "COMPLETED":
        return {"ok": True, "already_captured": True}
    try:
        result = await capture_order(order_id)
    except PaypalApiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    row.status = result.get("status", "COMPLETED")
    if row.status == "COMPLETED":
        row.captured_at = datetime.now(timezone.utc)
        u = db.get(User, user.user_id)
        if u:
            now = datetime.now(timezone.utc)
            # SQLite strips tzinfo on read; normalise before comparing.
            cur = u.entitlement_expires_at
            if cur is not None and cur.tzinfo is None:
                cur = cur.replace(tzinfo=timezone.utc)
            base = cur if cur and cur > now else now
            if (row.kind or "annual") == "monthly":
                u.entitlement_kind = "paypal_monthly_once"
                u.entitlement_expires_at = base + timedelta(days=30)
            else:
                u.entitlement_kind = "paypal_annual"
                u.entitlement_expires_at = base + timedelta(days=365)
    db.commit()
    return {"ok": True, "status": row.status, "kind": row.kind}


# ─── Monthly subscription ─────────────────────────────────────────────


class ActivateSubscriptionBody(BaseModel):
    subscription_id: str
    # "monthly" or "annual" — used to pick the entitlement_kind and the
    # fallback grace window if PayPal hasn't surfaced next_billing_time yet.
    plan: str = "monthly"


@router.post("/subscriptions/activated")
async def confirm_subscription(
    body: ActivateSubscriptionBody, user: RequireUser, db: Session = Depends(get_db)
) -> dict:
    """Called by the frontend after PayPal confirms a subscription approval
    (the JS Buttons SDK's onApprove fires with the subscription_id). We
    fetch the subscription details from PayPal and persist + grant
    entitlement. Webhook events will keep it up to date afterwards."""
    if user.kind == "sso":
        raise HTTPException(status_code=403, detail="SSO accounts already have admin rights")
    plan = (body.plan or "monthly").lower()
    if plan not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="plan must be 'monthly' or 'annual'")
    try:
        sub = await get_subscription(body.subscription_id)
    except PaypalApiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    existing = (
        db.query(PaypalSubscription)
        .filter_by(paypal_subscription_id=body.subscription_id)
        .first()
    )
    if not existing:
        existing = PaypalSubscription(
            user_id=user.user_id,
            paypal_subscription_id=body.subscription_id,
            paypal_payer_id=(sub.get("subscriber") or {}).get("payer_id"),
            plan=plan,
            status=sub.get("status", "ACTIVE"),
        )
        db.add(existing)
    else:
        existing.status = sub.get("status", existing.status)
        existing.plan = plan

    next_billing = (sub.get("billing_info") or {}).get("next_billing_time")
    if next_billing:
        try:
            existing.next_billing_at = datetime.fromisoformat(next_billing.replace("Z", "+00:00"))
        except ValueError:
            pass

    if existing.status == "ACTIVE":
        u = db.get(User, user.user_id)
        if u:
            # Subscription billing date is authoritative — extend the
            # entitlement to slightly past the next billing so a brief
            # webhook delay can't lock the user out. Fallback grace differs
            # by plan: 31 days for monthly, ~366 for annual.
            fallback_days = 31 if plan == "monthly" else 366
            target = existing.next_billing_at or (
                datetime.now(timezone.utc) + timedelta(days=fallback_days)
            )
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            u.entitlement_kind = "paypal_monthly" if plan == "monthly" else "paypal_annual_sub"
            u.entitlement_expires_at = target + timedelta(days=2)
    db.commit()
    return {"ok": True, "status": existing.status, "plan": plan}


@router.post("/subscriptions/cancel")
async def cancel_my_subscription(user: RequireUser, db: Session = Depends(get_db)) -> dict:
    """Cancel the user's active monthly subscription. They keep access
    until next_billing_at — we don't claw back what they've paid for."""
    sub = (
        db.query(PaypalSubscription)
        .filter_by(user_id=user.user_id, status="ACTIVE")
        .order_by(PaypalSubscription.created_at.desc())
        .first()
    )
    if not sub:
        raise HTTPException(status_code=404, detail="no active subscription")
    try:
        await cancel_subscription(sub.paypal_subscription_id)
    except PaypalApiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    sub.status = "CANCELLED"
    db.commit()
    return {"ok": True}


# ─── Webhook ─────────────────────────────────────────────────────────


@router.post("/webhook")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """PayPal events. Verifies the signature (rejects 401 on mismatch),
    then routes by event_type. We update the local subscription row;
    if it goes to a non-ACTIVE state past the user's entitlement_expires_at,
    we let the entitlement lapse naturally (no instant revoke — they paid
    for the period)."""
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not await verify_webhook(headers, body):
        raise HTTPException(status_code=401, detail="webhook signature verification failed")
    import json as _json

    try:
        event = _json.loads(body.decode("utf-8") or "{}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid JSON") from e

    etype = event.get("event_type", "")
    resource = event.get("resource") or {}
    sub_id = resource.get("id") if etype.startswith("BILLING.SUBSCRIPTION") else None

    if sub_id:
        row = (
            db.query(PaypalSubscription)
            .filter_by(paypal_subscription_id=sub_id)
            .first()
        )
        if row:
            if etype in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"):
                row.status = "CANCELLED" if etype.endswith("CANCELLED") else "EXPIRED"
            elif etype == "BILLING.SUBSCRIPTION.SUSPENDED":
                row.status = "SUSPENDED"
            elif etype in ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED"):
                row.status = "ACTIVE"
                # Extend entitlement to next billing time + grace.
                next_billing = (resource.get("billing_info") or {}).get("next_billing_time")
                if next_billing:
                    try:
                        nb = datetime.fromisoformat(next_billing.replace("Z", "+00:00"))
                        row.next_billing_at = nb
                        u = db.get(User, row.user_id)
                        if u:
                            u.entitlement_kind = (
                                "paypal_monthly" if row.plan == "monthly" else "paypal_annual_sub"
                            )
                            u.entitlement_expires_at = nb + timedelta(days=2)
                    except ValueError:
                        pass
            db.commit()

    return {"ok": True}


# ─── Billing history (Account page) ──────────────────────────────────


class BillingHistoryItem(BaseModel):
    """Unified row spanning PayPal orders, subscriptions, and redeemed
    vouchers. The frontend renders these as a chronological list. `kind`
    is the discriminator the SPA dispatches on; `label` is the localised
    plan name we already use in the Subscription card."""
    date: datetime
    kind: str  # "paypal_order_monthly" | "paypal_order_annual" |
               # "paypal_subscription_monthly" | "paypal_subscription_annual" |
               # "voucher"
    label: str
    amount: str | None = None
    currency: str | None = None
    status: str | None = None
    # Voucher code for voucher rows; PayPal id for paid rows. Helpful when
    # the user wants to cross-check a charge against their PayPal statement.
    reference: str | None = None


@router.get("/me/billing-history")
def my_billing_history(user: RequireUser, db: Session = Depends(get_db)) -> list[BillingHistoryItem]:
    """Aggregates everything that has ever granted this user
    meeting-creation rights: PayPal one-shot orders, PayPal subscriptions,
    and redeemed vouchers. Sorted newest-first. Returns [] for SSO users
    (their access comes from one.witysk.org, not from any local record)."""
    items: list[BillingHistoryItem] = []
    if user.kind == "sso":
        return items

    # PayPal one-shot orders
    orders = (
        db.query(PaypalOrder)
        .filter_by(user_id=user.user_id)
        .order_by(PaypalOrder.created_at.desc())
        .all()
    )
    for o in orders:
        kind = "paypal_order_monthly" if (o.kind or "annual") == "monthly" else "paypal_order_annual"
        label = "Monthly pass (one-time)" if kind == "paypal_order_monthly" else "Yearly pass (one-time)"
        items.append(
            BillingHistoryItem(
                date=o.captured_at or o.created_at,
                kind=kind,
                label=label,
                amount=o.amount,
                currency=o.currency,
                status=o.status,
                reference=o.paypal_order_id,
            )
        )

    # PayPal subscriptions
    subs = (
        db.query(PaypalSubscription)
        .filter_by(user_id=user.user_id)
        .order_by(PaypalSubscription.created_at.desc())
        .all()
    )
    for s in subs:
        plan = (s.plan or "monthly").lower()
        kind = "paypal_subscription_annual" if plan == "annual" else "paypal_subscription_monthly"
        label = (
            "Yearly subscription (auto-renew)"
            if plan == "annual"
            else "Monthly subscription (auto-renew)"
        )
        items.append(
            BillingHistoryItem(
                date=s.created_at,
                kind=kind,
                label=label,
                amount=None,
                currency="EUR",
                status=s.status,
                reference=s.paypal_subscription_id,
            )
        )

    # Vouchers — only the ones this user actually redeemed.
    vouchers = (
        db.query(Voucher)
        .filter(Voucher.redeemed_by_user_id == user.user_id)
        .order_by(Voucher.redeemed_at.desc())
        .all()
    )
    for v in vouchers:
        items.append(
            BillingHistoryItem(
                date=v.redeemed_at or v.created_at,
                kind="voucher",
                label=f"Voucher ({v.duration_days} days)",
                amount=None,
                currency=None,
                status="REDEEMED",
                reference=v.code,
            )
        )

    items.sort(key=lambda it: it.date, reverse=True)
    return items
