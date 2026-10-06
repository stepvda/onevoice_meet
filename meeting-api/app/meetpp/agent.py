"""HTTP client for meetpp-agent's session API (contract §6.1).

Docker-internal, no auth. Every method is best-effort and never raises, so a
missing agent degrades transcription but never breaks the board. Tests
replace `client` with a fake.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("app.meetpp")


class AgentClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base = (base_url or settings.meetpp_agent_url).rstrip("/")

    async def _request(self, method: str, path: str, *, json: dict | None = None, timeout: float = 10.0):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                return await c.request(method, f"{self.base}{path}", json=json)
        except Exception as exc:  # noqa: BLE001
            log.warning("meetpp-agent %s %s failed: %s", method, path, exc)
            return None

    async def start(self, payload: dict) -> bool:
        r = await self._request("POST", "/sessions", json=payload, timeout=15.0)
        # 409 = the agent already runs this session; treat as healthy.
        return r is not None and r.status_code in (200, 201, 202, 409)

    async def patch(self, sid: str, payload: dict) -> bool:
        r = await self._request("PATCH", f"/sessions/{sid}", json=payload)
        return r is not None and r.status_code < 400

    async def finalize(self, sid: str) -> bool:
        r = await self._request("POST", f"/sessions/{sid}/finalize", json={}, timeout=15.0)
        return r is not None and r.status_code in (200, 202)

    async def stop(self, sid: str) -> bool:
        r = await self._request("DELETE", f"/sessions/{sid}", timeout=15.0)
        return r is not None and r.status_code < 400

    async def tts(self, text: str, voice: str | None = None) -> dict | None:
        r = await self._request(
            "POST", "/tts", json={"text": text, "voice": voice or settings.meetpp_tts_voice}, timeout=30.0
        )
        if r is None or r.status_code >= 400:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("hash") else None

    async def health(self) -> dict | None:
        r = await self._request("GET", "/health", timeout=5.0)
        if r is None or r.status_code >= 400:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


client = AgentClient()


def session_health(health: dict | None, sid: str) -> dict | None:
    """The agent's /health entry for one session, or None when missing."""
    for entry in (health or {}).get("sessions") or []:
        if isinstance(entry, dict) and (entry.get("sid") or entry.get("session_id")) == sid:
            return entry
    return None
