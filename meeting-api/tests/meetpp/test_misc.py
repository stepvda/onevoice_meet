"""LLM gateway, message size cap, retention, egress transcript and ICS."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.db import SessionLocal
from app.meetpp import bus, llm, retention, util
from app.meetpp.models import MeetppLlmCall, MeetppSegment, MeetppSession
from app.services.ics import ics_invite

from .conftest import make_session, query


def test_state_message_caps_at_8kb_and_keeps_three_activations():
    acts = [
        {"kind": "topic", "tab": "agenda", "section_id": "s", "prio": 3},
        {"kind": "attendance", "tab": "attendance", "section_id": "s", "prio": 2},
        {"kind": "decision", "tab": "decisions", "section_id": "s", "item_id": "d", "prio": 5},
        {"kind": "action", "tab": "actions", "section_id": "s", "item_id": "a", "prio": 4},
    ]
    small = bus.state_message("sid", 3, {"decisions": [{"id": "d"}]}, acts)
    assert small["delta"] == {"decisions": [{"id": "d"}]}
    assert [a["prio"] for a in small["activations"]] == [5, 4, 3]
    big = bus.state_message("sid", 4, {"minutes": [{"id": "m", "narrative_md": "x" * 9000}]}, acts)
    assert "delta" not in big and big["version"] == 4 and len(big["activations"]) == 3
    assert len(bus.encode(big)) < 8192


def test_parse_json_tolerates_fences_and_prose():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        llm.parse_json("no json here")


async def test_complete_parsed_repairs_once(monkeypatch):
    replies = iter(['{"broken": ', '{"ok": true}'])
    seen = []

    async def fake(*, db, purpose, messages, max_tokens, temperature=0.2, session_id=None):
        seen.append(messages)
        return llm.LLMResult(text=next(replies), model="m")

    monkeypatch.setattr(llm, "complete_json", fake)
    parsed, _ = await llm.complete_parsed(db=None, purpose="tick", messages=[{"role": "user", "content": "q"}], max_tokens=10)
    assert parsed == {"ok": True}
    assert "could not be used" in seen[1][-1]["content"] and seen[1][-2]["role"] == "assistant"


async def test_breakers_are_per_purpose_and_errors_recorded(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_max_retries", 0)
    llm.reset_breakers()

    class Boom:
        async def complete(self, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(llm, "provider", lambda: Boom())
    sid = make_session()
    db = SessionLocal()
    try:
        for _ in range(5):
            with pytest.raises(llm.LLMError):
                await llm.complete_json(db=db, purpose="tick", messages=[], max_tokens=1, session_id=sid)
        assert llm.circuit_state("tick") == "open"
        assert llm.circuit_state("compose_section") == "closed"
        with pytest.raises(llm.LLMCircuitOpen):
            await llm.complete_json(db=db, purpose="tick", messages=[], max_tokens=1, session_id=sid)
        row = db.query(MeetppLlmCall).filter_by(session_id=sid).first()
        assert row.status == "error" and "refused" in row.error
        assert llm.ai_status(sid) == "paused"
    finally:
        db.close()
        llm.reset_breakers()


def test_budget_throttles_instead_of_stopping(monkeypatch):
    monkeypatch.setattr(settings, "meetpp_max_tokens_per_hour", 1000)
    sid = make_session()
    db = SessionLocal()
    try:
        db.add(MeetppLlmCall(session_id=sid, purpose="tick", prompt_tokens=1200))
        db.commit()
        assert llm.over_budget(db, sid)
    finally:
        db.close()


def test_retention_purges_unpublished_audio_after_seven_days(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "meetpp_data_dir", str(tmp_path))
    old = make_session()
    recent = make_session()
    db = SessionLocal()
    try:
        for sid, days in ((old, 8), (recent, 2)):
            s = db.get(MeetppSession, sid)
            s.status = "review"
            s.ended_at = util.now() - timedelta(days=days)
            (tmp_path / sid / "audio").mkdir(parents=True)
            (tmp_path / sid / "audio" / "u.ogg").write_bytes(b"x")
        db.commit()
    finally:
        db.close()
    tts = tmp_path / "tts"
    tts.mkdir()
    stale = tts / "aa.ogg"
    stale.write_bytes(b"x")
    past = time.time() - 9 * 86400
    os.utime(stale, (past, past))
    (tts / "bb.ogg").write_bytes(b"y")
    result = retention.run_retention()
    assert result["audio_dirs"] >= 1
    assert not (tmp_path / old / "audio").exists() and (tmp_path / recent / "audio").exists()
    assert not stale.exists() and (tts / "bb.ogg").exists()


def test_egress_transcript_uses_best_text_and_skips_gaps(tmp_path):
    from app.models import Recording
    from app.webhooks import _write_meetpp_transcript

    sid = make_session()
    db = SessionLocal()
    try:
        s = db.get(MeetppSession, sid)
        s.status = "review"
        s.ended_at = util.now()
        t = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
        db.add(MeetppSegment(session_id=sid, seq=1, identity="user-1", name="Alice", text="helo", text_refined="Hello.", tier=2, t_start=t))
        db.add(MeetppSegment(session_id=sid, seq=2, identity="meetpp-agent", text="", is_gap=True, gap_reason="liveness", t_start=t))
        rec = Recording(id=util.ulid(), meeting_id=s.meeting_id, egress_id="EG", file_path=str(tmp_path / "r.mp4"),
                        started_at=t, expires_at=t + timedelta(days=30), status="completed")
        db.add(rec)
        db.commit()
        assert _write_meetpp_transcript(db, rec)
        assert (tmp_path / "r.txt").read_text() == "[14:00:00] Alice: Hello."
    finally:
        db.rollback()
        db.close()


def test_ics_invite_has_request_sequence_and_attendee():
    start = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
    text = ics_invite(
        uid="meetpp-S1-20261010@meet.witysk.org",
        sequence=2,
        summary="Weekly Ops Sync",
        join_url="https://meet.witysk.org/amber-river-fox",
        dtstart=start,
        dtend=start + timedelta(hours=1),
        organizer_name="Chair",
        organizer_email="chair@example.org",
        attendees=[{"name": "Jan P.", "email": "jan@example.org"}],
        description_text="Agenda",
    )
    assert "METHOD:REQUEST" in text and "SEQUENCE:2" in text
    assert "ATTENDEE;CN=Jan P.;ROLE=REQ-PARTICIPANT" in text
    _ = query
