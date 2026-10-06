from __future__ import annotations

import asyncio
import json
import logging
import re
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from agent.metrics import iso
from agent.session import AgentSession
from agent.stt import STTResult
from agent.tier2 import Tier2Client
from agent.worker import WorkItem
from tests.fakes import FakePub, FakeParticipant, FakeRoom, Recorder, make_services, settle, tone

GB = 1024**3


def build_room(room: FakeRoom):
    alice = room.add(FakeParticipant("user-alice", "Alice"))
    a_mic = alice.add(FakePub("TR_a_mic", source=2))
    a_cam = alice.add(FakePub("TR_a_cam", source=1))
    bob = room.add(FakeParticipant("guest:bob", "Bob"))
    b_mic = bob.add(FakePub("TR_b_mic", source=2))
    egress = room.add(FakeParticipant("EG_rec", "Recorder", kind=2))
    e_mic = egress.add(FakePub("TR_e_mic", source=2))
    return SimpleNamespace(alice=alice, a_mic=a_mic, a_cam=a_cam, bob=bob, b_mic=b_mic, egress=egress, e_mic=e_mic)


class Prepopulated:
    """room_factory whose rooms already contain participants."""

    def __init__(self):
        self.rooms: list[FakeRoom] = []
        self.parts: list[SimpleNamespace] = []

    def __call__(self):
        room = FakeRoom()
        self.parts.append(build_room(room))
        self.rooms.append(room)
        return room


async def start_session(tmp_path, rec, *, accepted=("user-alice", "EG_rec"), tier2=None, **kw):
    services = make_services(tmp_path, rec, tier2=tier2, **kw)
    factory = Prepopulated()
    services.room_factory = factory
    s = AgentSession("S1", "room-1", "ws://lk", "tok-1", services=services, accepted=list(accepted), glossary="Meeting of OneVoice.")
    await s.start()
    return s, factory


async def test_subscribes_only_accepted_standard_microphones(tmp_path):
    rec = Recorder()
    s, f = await start_session(tmp_path, rec)
    p = f.parts[0]
    assert f.rooms[0].connected_with == ("ws://lk", "tok-1")
    assert p.a_mic.calls == [True]  # accepted, STANDARD, microphone
    assert p.a_cam.calls == []  # camera never
    assert p.b_mic.calls == []  # not accepted (default-deny)
    assert p.e_mic.calls == []  # egress is not a STANDARD participant
    f.rooms[0].subscribe_complete(p.alice, p.a_mic)
    await settle()
    assert set(s.consumers) == {"TR_a_mic"}
    # presence only for STANDARD participants
    await s.poster.drain(2)
    events = [e for b in rec.of("presence") for e in b["events"]]
    assert sorted((e["identity"], e["event"], e["kind"]) for e in events) == [
        ("guest:bob", "connected", "standard"),
        ("user-alice", "connected", "standard"),
    ]
    await s.close()


async def test_never_two_consumers_per_track(tmp_path):
    rec = Recorder()
    s, f = await start_session(tmp_path, rec)
    p = f.parts[0]
    f.rooms[0].subscribe_complete(p.alice, p.a_mic)
    f.rooms[0].emit("track_subscribed", p.a_mic.track, p.a_mic, p.alice)
    await settle()
    assert len(s.consumers) == 1
    first = s.consumers["TR_a_mic"]
    f.rooms[0].emit("track_unsubscribed", p.a_mic.track, p.a_mic, p.alice)
    await settle()
    assert s.consumers == {} and not first.running
    f.rooms[0].subscribe_complete(p.alice, p.a_mic)
    await settle()
    assert s.consumers["TR_a_mic"] is not first
    f.rooms[0].emit("track_unpublished", p.a_mic, p.alice)
    await settle()
    assert s.consumers == {}
    await s.close()


async def test_consent_patch_subscribes_and_unsubscribes(tmp_path):
    rec = Recorder()
    s, f = await start_session(tmp_path, rec, accepted=["user-alice"])
    p, room = f.parts[0], f.rooms[0]
    room.subscribe_complete(p.alice, p.a_mic)
    await settle()
    consumer = s.consumers["TR_a_mic"]
    s.set_accepted(["guest:bob"])  # Alice opts out, Bob accepts
    await settle()
    assert p.a_mic.calls == [True, False]
    assert "TR_a_mic" not in s.consumers and not consumer.running
    assert p.b_mic.calls == [True]
    room.subscribe_complete(p.bob, p.b_mic)
    await settle()
    assert set(s.consumers) == {"TR_b_mic"}
    # a late subscription for a non-consented track is refused, never consumed
    room.emit("track_subscribed", object(), p.a_mic, p.alice)
    assert p.a_mic.calls[-1] is False and "TR_a_mic" not in s.consumers
    # the periodic re-send re-asserts a lost subscription
    p.b_mic.subscribed = False
    s.set_accepted(["guest:bob"])
    assert p.b_mic.calls[-1] is True
    # a new participant who accepted later gets subscribed when published
    carol = room.add(FakeParticipant("user-carol", "Carol"))
    s.set_accepted(["guest:bob", "user-carol"])
    c_mic = carol.add(FakePub("TR_c_mic"))
    room.emit("track_published", c_mic, carol)
    assert c_mic.calls == [True]
    await s.close()


async def test_reconnect_with_fresh_token_and_gap(tmp_path, caplog):
    rec = Recorder()
    s, f = await start_session(tmp_path, rec)
    room1 = f.rooms[0]
    room1.subscribe_complete(f.parts[0].alice, f.parts[0].a_mic)
    await settle()
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        room1.emit("disconnected", "SERVER_SHUTDOWN")
        assert s.status_value() == "reconnecting" and not s.connected
        for _ in range(100):
            await asyncio.sleep(0.01)
            if s.connected and not s.reconnecting:
                break
    assert s.connected and s.status_value() == "listening"
    assert rec.of("agent-token") == [{}]
    room2 = f.rooms[1]
    assert room2.connected_with == ("ws://lk", "fresh-token")
    assert f.parts[1].a_mic.calls == [True]  # resubscribed in the new room
    await s.poster.drain(2)
    gaps = rec.items("gaps")
    assert len(gaps) == 1 and gaps[0]["reason"] == "agent_reconnect" and "identity" not in gaps[0]
    assert any("reconnected after" in r.message for r in caplog.records)
    # events from the dead room are ignored
    room1.emit("track_subscribed", object(), f.parts[0].b_mic, f.parts[0].bob)
    await s.close()


async def test_reconnect_gives_up_when_session_is_gone(tmp_path):
    rec = Recorder()
    rec.responses["agent-token"] = [(404, {"detail": "not found"})]
    s, f = await start_session(tmp_path, rec)
    f.rooms[0].emit("disconnected", None)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if s.gave_up:
            break
    assert s.gave_up and s.status_value() == "offline"
    await s.close()


async def test_liveness_restarts_silent_consumer(tmp_path, caplog):
    now = [1000.0]
    rec = Recorder()
    s, f = await start_session(tmp_path, rec, clock=lambda: now[0])
    p, room = f.parts[0], f.rooms[0]
    room.subscribe_complete(p.alice, p.a_mic)
    await settle()
    c = s.consumers["TR_a_mic"]
    await asyncio.sleep(0.05)
    assert c.alive
    restarted = []
    c.restart = lambda reason: restarted.append(reason)
    c.started_at, c.last_activity = 1000.0, 0.0
    s._resumed_at = 0.0
    now[0] = 1005.0
    room.emit("active_speakers_changed", [p.alice])
    now[0] = 1012.0
    assert s.check_liveness() == []  # only 7 s of speaking so far
    now[0] = 1025.0
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        assert s.check_liveness() == ["TR_a_mic"]
    assert restarted == ["liveness"]
    assert s.speaker_ok["user-alice"] is False
    assert s.speakers() == [{"identity": "user-alice", "name": "Alice", "ok": False}]
    assert any("MEETPP_LIVENESS" in r.message for r in caplog.records)
    await s.poster.drain(2)
    assert rec.items("gaps")[-1]["reason"] == "liveness"
    # VAD activity within the window → no restart
    c.started_at = 1000.0
    c.last_activity = 1024.0
    assert s.check_liveness() == []
    await s.close()


async def test_stt_results_drops_and_gaps(tmp_path, caplog):
    rec = Recorder()
    s, f = await start_session(tmp_path, rec)
    item = WorkItem(
        session_id="S1", utterance_id="a" * 32, identity="user-alice", name="Alice",
        audio=tone(16000), t_start=1_790_000_000.0, t_end=1_790_000_001.0,
        on_result=s._on_stt_result, on_drop=s._on_stt_drop,
    )
    s._stt_outstanding = 3
    with caplog.at_level(logging.INFO, logger="meetpp.agent"):
        s._on_stt_result(item, STTResult(dropped="hallucination", detail="Thank you.", audio_s=1, elapsed_s=0.2))
        s._on_stt_drop(item, "overloaded")
        s._on_stt_result(item, STTResult(text="The budget is approved.", avg_logprob=-0.21, no_speech_prob=0.01, audio_s=1, elapsed_s=0.2))
    assert s.dropped == {"hallucination": 1, "overloaded": 1}
    assert s.decoded == 1
    assert any("reason=hallucination" in r.message for r in caplog.records)
    assert any("queue drop" in r.message and r.levelno == logging.WARNING for r in caplog.records)
    await s.poster.drain(2)
    segs, gaps = rec.items("segments"), rec.items("gaps")
    assert segs == [
        {
            "utterance_id": "a" * 32, "identity": "user-alice", "name": "Alice",
            "t_start": iso(1_790_000_000.0), "t_end": iso(1_790_000_001.0),
            "text": "The budget is approved.", "lang": "en", "avg_logprob": -0.21, "no_speech_prob": 0.01,
        }
    ]
    assert gaps == [{"identity": "user-alice", "name": "Alice", "t_from": iso(1_790_000_000.0), "t_to": iso(1_790_000_001.0), "reason": "overloaded"}]
    assert "The budget is approved." in s._prompt()
    await asyncio.sleep(0.2)
    assert (tmp_path / "S1" / "audio" / f"{'a' * 32}.ogg").exists()  # stored after decode
    line = s.summary_line()
    assert re.match(
        r"MEETPP_STT sid=S1 speakers=\d+ consumers_alive=\d+ utterances=\d+ decoded=1 dropped=2\(hallucination:1,overloaded:1\) "
        r"backlog_s=\d+\.\d rtf_p50=0\.20 rtf_p95=0\.20 tier2_ok=0 tier2_fail=0 last_segment_age_s=\d+$",
        line,
    ), line
    await s.close()


async def test_utterance_ids_and_end_to_end_with_fake_stt(tmp_path):
    """Audio in → VAD → worker → segment posted with a uuid4 hex id."""
    rec = Recorder()

    async def speech_stream(track):
        a = np.concatenate([np.zeros(8000, np.float32), tone(24000), np.zeros(24000, np.float32)])
        for i in range(0, len(a), 160):
            await asyncio.sleep(0)
            yield a[i : i + 160]
        while True:
            await asyncio.sleep(0.01)
            yield np.zeros(160, np.float32)

    s, f = await start_session(tmp_path, rec, stream_factory=speech_stream)
    s.services.worker._dispatch = lambda fn, *a: fn(*a)
    p = f.parts[0]
    f.rooms[0].subscribe_complete(p.alice, p.a_mic)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if s.services.worker.pending():
            break
    w = s.services.worker
    w.process_one(w.next_item())
    await s.poster.drain(2)
    seg = rec.items("segments")[0]
    assert re.fullmatch(r"[0-9a-f]{32}", seg["utterance_id"])
    assert seg["identity"] == "user-alice" and seg["text"] == "hello world"
    assert s.utterances == 1 and s.speakers() == [{"identity": "user-alice", "name": "Alice", "ok": True}]
    await s.close()


async def test_status_body_and_disk_guard(tmp_path):
    rec = Recorder()
    t2 = Tier2Client("http://mac", "x", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))))
    await t2.check_health()
    s, f = await start_session(tmp_path, rec, tier2=t2)
    body = s.status_body()
    assert body == {"status": "listening", "backlog_s": 0.0, "rtf_p50": None, "speakers": [], "tier2": "up"}
    s.store.ok = False  # disk guard tripped
    assert s.status_body()["tier2"] == "down"
    s.set_paused(True)
    assert s.status_value() == "paused"
    assert s.health()["paused"] is True and s.health()["connected"] is True
    await s.close()
    assert rec.of("agent-status")[-1]["status"] == "offline"


async def test_finalize_without_tier2_is_skipped(tmp_path):
    rec = Recorder()
    s, f = await start_session(tmp_path, rec)
    assert s.finalize() == "skipped"
    await asyncio.wait_for(s._final_task, 5)
    await s.poster.drain(2)
    assert rec.of("agent-status")[-1]["final_pass"] == "skipped"
    assert f.rooms[0].disconnected and s.status_value() == "offline"
    await s.close()


async def test_finalize_final_pass_in_time_order_with_per_speaker_context(tmp_path):
    rec = Recorder()
    prompts: list[tuple[str, str]] = []
    texts = {
        "u1": "Alice first refined.",
        "u2": "Bob first refined.",
        "u3": "Alice second refined.",
        "u4": "loop loop loop loop loop loop loop loop loop loop",
    }
    order: list[str] = []
    busy_once: set[str] = set()

    async def speech_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        uid = request.content.decode().split(":")[1]
        if uid == "u2" and "u2" not in busy_once:
            busy_once.add("u2")  # queue full once: must retry after Retry-After
            return httpx.Response(503, headers={"Retry-After": "0"})
        order.append(uid)
        prompts.append((uid, request.url.params["prompt"]))
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"text": texts[uid], "repetition": False})

    t2 = Tier2Client("http://mac", "x", httpx.AsyncClient(transport=httpx.MockTransport(speech_handler)))
    await t2.check_health()
    s, f = await start_session(tmp_path, rec, tier2=t2)
    s.store.read = lambda uid: f"OggS:{uid}".encode()
    for uid, who, t0, text in [
        ("u3", "user-alice", "10:00:09", "alice two"),
        ("u1", "user-alice", "10:00:01", "alice one"),
        ("u2", "guest:bob", "10:00:03", "bob one"),
        ("u4", "guest:bob", "10:00:12", "bob two"),
    ]:
        s.store.write(uid, tone(8000), identity=who, name=who, t_start=f"2026-10-06T{t0}.000Z", t_end=f"2026-10-06T{t0}.500Z", text=text)
    assert s.finalize() == "running"
    await asyncio.wait_for(s._final_task, 10)
    await s.poster.drain(2)
    assert busy_once == {"u2"}
    assert order[:2] == ["u1", "u2"]  # time order, one request per speaker at a time
    assert sorted(order) == ["u1", "u2", "u3", "u4"]
    by_uid = dict(prompts)
    assert by_uid["u3"].startswith("Meeting of OneVoice.") and by_uid["u3"].endswith("Alice first refined.")
    assert "Bob" not in by_uid["u3"]  # context is per speaker
    assert by_uid["u4"].endswith("Bob first refined.")
    refinements = rec.items("refinements")
    assert {r["utterance_id"]: r["text"] for r in refinements} == {u: texts[u] for u in ("u1", "u2", "u3")}  # u4 loop rejected
    assert all(r["final"] is True for r in refinements)
    # the final status comes after every refinement
    last_seg_idx = max(i for i, (e, b) in enumerate(rec.requests) if e == "segments" and b.get("refinements"))
    status_idx = [i for i, (e, b) in enumerate(rec.requests) if e == "agent-status" and b.get("final_pass") == "done"]
    assert status_idx and status_idx[0] > last_seg_idx
    await s.close()


async def test_finalize_fails_when_tier2_down(tmp_path):
    rec = Recorder()

    def handler(r):
        raise httpx.ConnectError("mac asleep")

    t2 = Tier2Client("http://mac", "x", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    s, f = await start_session(tmp_path, rec, tier2=t2)
    s.finalize()
    await asyncio.wait_for(s._final_task, 5)
    await s.poster.drain(2)
    assert rec.of("agent-status")[-1]["final_pass"] == "failed"
    await s.close()


async def test_near_live_refinement_and_repetition_guard(tmp_path):
    rec = Recorder()
    replies = iter([{"text": "The budget is approved by the board."}, {"text": "say say say say say say say say say", "repetition": False}])

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=next(replies))

    t2 = Tier2Client("http://mac", "x", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await t2.check_health()
    s, f = await start_session(tmp_path, rec, tier2=t2)
    for uid in ("b" * 32, "c" * 32):
        item = WorkItem(session_id="S1", utterance_id=uid, identity="user-alice", name="Alice", audio=tone(16000),
                        t_start=1_790_000_000.0, t_end=1_790_000_001.0, on_result=s._on_stt_result, on_drop=s._on_stt_drop)
        s._on_stt_result(item, STTResult(text="the budget is approved", audio_s=1, elapsed_s=0.2))
        await asyncio.wait_for(asyncio.gather(*list(s._store_tasks)), 5)
    await s.poster.drain(2)
    refs = rec.items("refinements")
    assert refs == [{"utterance_id": "b" * 32, "text": "The budget is approved by the board.", "final": False}]
    assert s.tier2_ok == 2 and s.tier2_kept_t1 == 1
    await s.close()


async def test_near_live_drops_on_503_without_retry(tmp_path):
    rec = Recorder()
    calls = []

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "2"})

    t2 = Tier2Client("http://mac", "x", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await t2.check_health()
    s, f = await start_session(tmp_path, rec, tier2=t2)
    item = WorkItem(session_id="S1", utterance_id="d" * 32, identity="user-alice", name="Alice", audio=tone(16000),
                    t_start=1_790_000_000.0, t_end=1_790_000_001.0, on_result=s._on_stt_result, on_drop=s._on_stt_drop)
    s._on_stt_result(item, STTResult(text="tier one text", audio_s=1, elapsed_s=0.2))
    await asyncio.wait_for(asyncio.gather(*list(s._store_tasks)), 5)
    await s.poster.drain(2)
    assert len(calls) == 1 and rec.items("refinements") == [] and s.tier2_fail == 1
    assert len(rec.items("segments")) == 1  # tier 1 stands
    await s.close()
