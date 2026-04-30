"""
PayPal REST API wrapper.

Uses the v2 Orders API (one-shot annual purchase) and v1 Subscriptions API
(€2/month recurring). Tokens are cached in-memory for the duration of their
~9-hour lifetime; a single client_credentials grant carries every API call
the meet backend makes on behalf of the merchant.

We deliberately don't use the official `paypalrestsdk` package — it's
been deprecated for years; httpx + REST is simpler and supported.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.config import settings


class PaypalNotConfigured(RuntimeError):
    """Raised when PAYPAL_CLIENT_ID / SECRET haven't been set in .env."""


class PaypalApiError(RuntimeError):
    """Raised when PayPal returns a non-2xx response."""

    def __init__(self, status: int, body: str):
        super().__init__(f"PayPal {status}: {body[:200]}")
        self.status = status
        self.body = body


_TOKEN: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_TOKEN_LOCK = asyncio.Lock()


def _require_creds() -> tuple[str, str]:
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise PaypalNotConfigured(
            "PayPal billing is not configured. Set PAYPAL_CLIENT_ID and "
            "PAYPAL_CLIENT_SECRET in .env from the PayPal Business "
            "developer dashboard."
        )
    return settings.paypal_client_id, settings.paypal_client_secret


async def _get_access_token() -> str:
    """Fetch (and cache) an access token via the client_credentials grant."""
    if _TOKEN["access_token"] and _TOKEN["expires_at"] > time.time() + 60:
        return _TOKEN["access_token"]
    async with _TOKEN_LOCK:
        if _TOKEN["access_token"] and _TOKEN["expires_at"] > time.time() + 60:
            return _TOKEN["access_token"]
        client_id, client_secret = _require_creds()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.paypal_api_base}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise PaypalApiError(resp.status_code, resp.text)
        body = resp.json()
        _TOKEN["access_token"] = body["access_token"]
        _TOKEN["expires_at"] = time.time() + int(body.get("expires_in", 3600))
        return _TOKEN["access_token"]


async def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    token = await _get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.request(
            method, f"{settings.paypal_api_base}{path}", headers=headers, json=json_body
        )
    if resp.status_code >= 400:
        raise PaypalApiError(resp.status_code, resp.text)
    if not resp.content:
        return {}
    return resp.json()


# ─── Orders (one-shot annual purchase) ────────────────────────────────


async def create_order(amount: str, currency: str, return_url: str, cancel_url: str) -> dict:
    """Create a v2 Order. The buyer approves it via PayPal's hosted flow,
    then we capture it server-side to actually move the money."""
    return await _request(
        "POST",
        "/v2/checkout/orders",
        {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {"currency_code": currency, "value": amount},
                    "description": "meet.witysk.org — 1 year of meeting-creation rights",
                }
            ],
            "application_context": {
                "brand_name": "meet.witysk.org",
                "user_action": "PAY_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        },
    )


async def capture_order(order_id: str) -> dict:
    return await _request("POST", f"/v2/checkout/orders/{order_id}/capture")


async def get_order(order_id: str) -> dict:
    return await _request("GET", f"/v2/checkout/orders/{order_id}")


# ─── Subscriptions (monthly recurring) ────────────────────────────────


async def get_subscription(sub_id: str) -> dict:
    return await _request("GET", f"/v1/billing/subscriptions/{sub_id}")


async def cancel_subscription(sub_id: str, reason: str = "user requested") -> None:
    await _request(
        "POST",
        f"/v1/billing/subscriptions/{sub_id}/cancel",
        {"reason": reason[:128]},
    )


# ─── Webhook signature verification ──────────────────────────────────


async def verify_webhook(headers: dict[str, str], body: bytes) -> bool:
    """Asks PayPal whether a received webhook is genuine. Without
    PAYPAL_WEBHOOK_ID set, we can't verify and refuse rather than accept
    blindly (returning False rejects)."""
    if not settings.paypal_webhook_id:
        return False
    payload = {
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
        "webhook_id": settings.paypal_webhook_id,
        "webhook_event": json.loads(body.decode("utf-8") or "{}"),
    }
    try:
        result = await _request("POST", "/v1/notifications/verify-webhook-signature", payload)
    except PaypalApiError:
        return False
    return result.get("verification_status") == "SUCCESS"
