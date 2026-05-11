"""
Email service using Resend.

Mirrors the pattern used in onevoice's `app/services/email_service.py`:
HTTP API via httpx async, single + batch sends, structured logging.

The visual style of templates here uses meet's "dark grey to very dark blue"
palette (matching the in-app primary-* and accent-* tokens).
"""
import logging
from typing import List, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


async def send_email(
    *,
    to: str | List[str],
    subject: str,
    html: str,
    text: Optional[str] = None,
    cc: Optional[str | List[str]] = None,
    bcc: Optional[str | List[str]] = None,
    reply_to: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> bool:
    """Send an email via Resend. Returns True on success.

    No-ops (returns False) when RESEND_API_KEY is unconfigured so that
    development environments don't crash on missing creds.
    """
    if not settings.resend_api_key:
        log.warning("EMAIL_SKIP reason=no_api_key_configured to=%s subject=%s", to, subject)
        return False

    if isinstance(to, str):
        to = [to]

    payload: dict = {
        "from": settings.from_email,
        "to": to,
        "subject": subject,
        "html": html,
        "text": text or "",
    }
    if cc:
        payload["cc"] = [cc] if isinstance(cc, str) else cc
    if bcc:
        payload["bcc"] = [bcc] if isinstance(bcc, str) else bcc
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        # Resend's attachments schema: [{filename, content (base64), content_type?}]
        payload["attachments"] = attachments

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            log.info("EMAIL_SENT to=%s subject=%s", to, subject)
            return True
    except httpx.HTTPStatusError as e:
        log.error("EMAIL_FAIL to=%s subject=%s status=%s body=%s", to, subject, e.response.status_code, e.response.text[:300])
        return False
    except Exception as e:  # noqa: BLE001
        log.error("EMAIL_FAIL to=%s subject=%s error=%s", to, subject, e)
        return False
