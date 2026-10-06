from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx
import pytest

from agent.poster import InternalApi, Poster, dumps, sign
from tests.fakes import SECRET


def meeting_api_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Copy of meeting-api app/meetpp/auth.py internal_signature()."""
    msg = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def test_sign_matches_meeting_api_scheme():
    body = dumps({"segments": [{"utterance_id": "abc", "text": "Café ✓"}]})
    h = sign("s3cret", body, ts=1700000000)
    assert h["X-Meetpp-Timestamp"] == "1700000000"
    assert h["X-Meetpp-Signature"] == meeting_api_signature("s3cret", "1700000000", body)
    # fixed vector: hex(HMAC_SHA256("k", "1." + "{}"))
    assert sign("k", b"{}", ts=1)["X-Meetpp-Signature"] == hmac.new(b"k", b"1.{}", hashlib.sha256).hexdigest()
    assert abs(int(sign("k", b"")["X-Meetpp-Timestamp"]) - time.time()) < 2


class Script:
    """MockTransport handler: verifies HMAC, records, replies from a script."""

    def __init__(self, statuses: list[int] | None = None, default: int = 200) -> None:
        self.statuses = list(statuses or [])
        self.default = default
        self.calls: list[tuple[str, dict, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        ts = request.headers["X-Meetpp-Timestamp"]
        assert request.headers["X-Meetpp-Signature"] == meeting_api_signature(SECRET, ts, request.content)
        assert abs(int(ts) - time.time()) <= 60
        status = self.statuses.pop(0) if self.statuses else self.default
        endpoint = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, json.loads(request.content), status))
        if status == -1:
            raise httpx.ConnectError("refused")
        return httpx.Response(status, json={"ok": status < 400, "seqs": {}})


def make_poster(handler, **kw) -> Poster:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Poster(InternalApi(client, "http://api", SECRET), "sid1", backoff=(0.0,), **kw)


async def test_url_and_body_shape():
    script = Script()
    p = make_poster(script)
    p.start()
    p.segment({"utterance_id": "u1", "text": "hi"})
    assert await p.drain(2)
    await p.close()
    endpoint, body, _ = script.calls[0]
    assert endpoint == "segments" and body == {"segments": [{"utterance_id": "u1", "text": "hi"}]}


async def test_retry_keeps_order_and_batches(caplog):
    script = Script([503, -1, 200, 200, 200])
    p = make_poster(script)
    p.segment({"utterance_id": "u1"})
    p.segment({"utterance_id": "u2"})
    p.refinement({"utterance_id": "u1", "text": "x", "final": False})
    p.gap({"t_from": "a", "t_to": "b", "reason": "overloaded"})
    p.segment({"utterance_id": "u3"})
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        p.start()
        assert await p.drain(3)
    await p.close()
    sent = [(e, b, s) for e, b, s in script.calls]
    # same batch retried after 503 and a network error, then the rest in order
    assert [s for _, _, s in sent] == [503, -1, 200, 200, 200, 200]
    assert sent[0][1] == sent[1][1] == sent[2][1] == {"segments": [{"utterance_id": "u1"}, {"utterance_id": "u2"}]}
    assert sent[3][1] == {"refinements": [{"utterance_id": "u1", "text": "x", "final": False}]}
    assert sent[4][1] == {"gaps": [{"t_from": "a", "t_to": "b", "reason": "overloaded"}]}
    assert sent[5][1] == {"segments": [{"utterance_id": "u3"}]}
    assert p.retries == 2 and p.sent == 5
    assert any("retrying in order" in r.message for r in caplog.records)


async def test_4xx_is_logged_and_never_retried(caplog):
    script = Script([422, 200])
    p = make_poster(script)
    p.presence({"identity": "user-a", "event": "connected"})
    p.segment({"utterance_id": "u1"})
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        p.start()
        assert await p.drain(2)
    await p.close()
    assert [(e, s) for e, _, s in script.calls] == [("presence", 422), ("segments", 200)]
    assert p.rejected == 1
    assert any("not retried" in r.message for r in caplog.records)


async def test_retry_window_expires_with_warning(caplog):
    now = [0.0]
    script = Script(default=500)
    p = make_poster(script, clock=lambda: now[0])
    p.segment({"utterance_id": "old"})
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        p.start()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(script.calls) >= 2  # retried while within the window
        now[0] = 301.0
        script.default = 200
        p.segment({"utterance_id": "new"})
        assert await p.drain(2)
    await p.close()
    delivered = [b for _, b, s in script.calls if s == 200]
    assert delivered == [{"segments": [{"utterance_id": "new"}]}]
    assert p.expired == 1
    assert any("retry window" in r.message for r in caplog.records)


async def test_post_status_is_best_effort_not_buffered():
    script = Script([500])
    p = make_poster(script)
    assert await p.post_status({"status": "listening"}) is False
    assert p.backlog == 0
    assert await p.post_status({"status": "listening"}) is True
    assert [e for e, _, _ in script.calls] == ["agent-status", "agent-status"]


async def test_ordered_status_goes_after_refinements():
    script = Script()
    p = make_poster(script)
    p.refinement({"utterance_id": "u1", "text": "a", "final": True})
    p.status({"status": "offline", "final_pass": "done"})
    p.start()
    assert await p.drain(2)
    await p.close()
    assert [e for e, _, _ in script.calls] == ["segments", "agent-status"]
    assert script.calls[1][1]["final_pass"] == "done"
