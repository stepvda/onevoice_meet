"""Replay a recorded meeting through the Meet++ pipeline with the real LLM.

Dry run for prompts, interpretation, composition and the report, without
LiveKit or the agent: a whisper-style JSON transcript is ingested on a
simulated meeting clock, ticks run every MEETPP_TICK_SECONDS of meeting time,
sections close when the AI (lead mode) moves on, and the session is finalised
and rendered to the meeting report PDF.

Everything runs against a throw-away SQLite database. Speaker labels are not
in a plain whisper transcript, so all speech is attributed to one speaker
unless the JSON segments carry a "speaker" field.

Usage (from meeting-api/, with LLM_API_KEY in the environment):

    .venv/bin/python tools/meetpp_replay.py \
        --transcript m240.json --agenda agenda.pdf --previous report.pdf \
        --start 2026-10-04T14:04:00Z --out ./replay-out
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _env(out: Path) -> None:
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{out / 'replay.db'}")
    os.environ.setdefault("MEETPP_DATA_DIR", str(out / "data"))
    os.environ.setdefault("RECORDINGS_DIR", str(out / "rec"))
    os.environ.setdefault("LOG_DIR", str(out / "logs"))
    os.environ.setdefault("JWT_SECRET_KEY", "replay")
    os.environ.setdefault("LIVEKIT_API_KEY", "replay")
    os.environ.setdefault("LIVEKIT_API_SECRET", "replay-secret-replay-secret-replay")
    os.environ.setdefault("LIVEKIT_WEBHOOK_KEY", "replay")
    os.environ.setdefault("MEETPP_ENABLED", "true")
    os.environ.setdefault("MEETPP_INTERNAL_SECRET", "replay")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/15")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True, help="whisper JSON (segments[].start/end/text[/speaker])")
    ap.add_argument("--agenda")
    ap.add_argument("--previous")
    ap.add_argument("--start", required=True, help="meeting start, ISO UTC")
    ap.add_argument("--out", required=True)
    ap.add_argument("--meeting-type", default="board")
    ap.add_argument("--title", default="Founders meeting (replay)")
    ap.add_argument("--people", default="", help="comma list 'sub:Name' joining the meeting")
    ap.add_argument("--speaker", default="Meeting audio", help="name used when segments have no speaker")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop after N minutes of meeting time (0 = all)")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    _env(out)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app.config import settings
    from app.db import SessionLocal, engine
    from app.meetpp import agent as agent_mod
    from app.meetpp import bus, compose, export, ingest, llm, outline, runtime as rt, util
    from app.meetpp.models import (
        MeetppAction, MeetppDecision, MeetppDocument, MeetppMinute, MeetppOp, MeetppSection,
        MeetppSeries, MeetppSession, ensure_schema,
    )
    from app.models import Base, Meeting

    if not settings.llm_api_key:
        print("LLM_API_KEY is required", file=sys.stderr)
        return 2
    ensure_schema(engine)
    Base.metadata.create_all(bind=engine)

    # ── simulated clock ────────────────────────────────────────────────────
    t0 = datetime.fromisoformat(args.start.replace("Z", "+00:00")).astimezone(timezone.utc)
    clock = {"t": t0 - timedelta(minutes=5)}
    util.now = lambda: clock["t"]  # type: ignore[assignment]

    # ── fakes for LiveKit and the agent ────────────────────────────────────
    sent: list[dict] = []

    async def _send(room, msg, destination_identities=None):
        sent.append(msg)
        return True

    async def _set_room(room, payload, *, board=None):
        return True

    class _Agent:
        async def start(self, payload): return True
        async def patch(self, sid, payload): return True
        async def finalize(self, sid): return True
        async def stop(self, sid): return True
        async def tts(self, text, voice=None): return None
        async def health(self): return {"ok": True, "sessions": [{"sid": sid_box["sid"], "connected": True}]}

    sid_box: dict = {}
    bus.send = _send  # type: ignore[assignment]
    bus.set_room_meetpp = _set_room  # type: ignore[assignment]
    agent_mod.client = _Agent()  # type: ignore[assignment]
    pending_compose: list[tuple[str, str, bool]] = []

    def _schedule(session_id, section_id, *, delay=0.0, force=False):
        pending_compose.append((session_id, section_id, force))
        return None

    compose.schedule_section = _schedule  # type: ignore[assignment]
    # The replay drives ticks and finalisation itself: no background runners.
    rt.runtime.start_session = lambda session_id: None  # type: ignore[assignment]
    rt.runtime.start_finalisation = lambda session_id: None  # type: ignore[assignment]

    # Every tick's raw LLM output, for diagnosing navigation and extraction.
    tick_log = (out / "ticks.jsonl").open("w")
    real_complete = llm.complete_parsed

    async def _complete(*a, **kw):
        parsed, result = await real_complete(*a, **kw)
        if kw.get("purpose") == "tick":
            user = kw["messages"][-1]["content"]
            new = user.split("NEW (cite at least one of these):\n", 1)[-1]
            tick_log.write(json.dumps({"t": util.iso(clock["t"]), "new": new[:1500], "out": parsed}, default=str) + "\n")
            tick_log.flush()
        return parsed, result

    llm.complete_parsed = _complete  # type: ignore[assignment]

    async def run_compositions():
        while pending_compose:
            s_id, sec_id, force = pending_compose.pop(0)
            await compose.compose_section(s_id, sec_id, force=force)

    # ── meeting, series, session ───────────────────────────────────────────
    db = SessionLocal()
    meeting = Meeting(id=util.ulid(), room_name="replay-room", display_title=args.title,
                      owner_user_id="chair", owner_name="Chair", owner_email="chair@example.org",
                      scheduled_at=t0)
    db.add(meeting)
    series = MeetppSeries(id=util.ulid(), owner_sub="chair", title=args.title, meeting_id=meeting.id,
                          meeting_type=args.meeting_type)
    db.add(series)
    db.flush()
    meeting.meetpp_series_id = series.id
    session = MeetppSession(id=util.ulid(), meeting_id=meeting.id, series_id=series.id,
                            created_by_user_id="chair", status="setup", template="agenda", mode="lead",
                            settings_json=util.dumps({"show_public": False, "in_recordings": True,
                                                      "timebox_nudges": True, "speak": True}))
    db.add(session)
    db.flush()
    outline.create_fixed_sections(db, session)
    db.commit()
    sid = session.id
    sid_box["sid"] = sid
    db.close()

    async def add_doc(path: str, kind: str):
        data_dir = Path(settings.meetpp_data_dir) / sid / "uploads"
        data_dir.mkdir(parents=True, exist_ok=True)
        dst = data_dir / Path(path).name
        shutil.copy(path, dst)
        db = SessionLocal()
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind=kind, filename=Path(path).name, path=str(dst))
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()
        await ingest.process_document(doc_id)
        db = SessionLocal()
        doc = db.get(MeetppDocument, doc_id)
        print(f"[doc] {kind}: status={doc.status} error={doc.error} summary={doc.summary_json}")
        db.close()

    if args.previous:
        await add_doc(args.previous, "previous_notes")
    if args.agenda:
        await add_doc(args.agenda, "agenda")

    # ── start, people join ────────────────────────────────────────────────
    clock["t"] = t0
    await rt.start_session(sid)
    people = [p.split(":", 1) for p in args.people.split(",") if ":" in p]
    for sub, name in people:
        ident = f"user-{sub}"
        await rt.presence(sid, [{"identity": ident, "name": name, "kind": "standard", "event": "connected"}])
        await rt.set_consent(sid, ident, "accept", f"sub:{sub}", name)
    speaker_ident = "user-replay-audio"
    await rt.presence(sid, [{"identity": speaker_ident, "name": args.speaker, "kind": "standard", "event": "connected"}])
    await rt.set_consent(sid, speaker_ident, "accept", "sub:replay-audio", args.speaker)

    # ── replay ─────────────────────────────────────────────────────────────
    segs = json.loads(Path(args.transcript).read_text())["segments"]
    tick_s = float(settings.meetpp_tick_seconds)
    next_tick = t0 + timedelta(seconds=tick_s)
    stats = {"ticks": 0, "applied": 0, "rejected": 0, "notes": 0, "moves": 0}
    timeline = []
    n_sent = len(sent)
    for i, seg in enumerate(segs):
        st = t0 + timedelta(seconds=float(seg["start"]))
        en = t0 + timedelta(seconds=float(seg["end"]))
        if args.max_minutes and (st - t0).total_seconds() > args.max_minutes * 60:
            break
        while next_tick <= st:
            clock["t"] = next_tick
            res = await rt.tick(sid)
            if res and not res.get("skipped"):
                stats["ticks"] += 1
                stats["applied"] += int(res.get("applied") or 0)
                stats["rejected"] += len(res.get("rejected") or [])
                stats["notes"] += int(res.get("notes") or 0)
            await run_compositions()
            next_tick += timedelta(seconds=tick_s)
        clock["t"] = en
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        name = seg.get("speaker") or args.speaker
        ident = speaker_ident if not seg.get("speaker") else f"user-{name.lower().replace(' ', '-')}"
        await rt.ingest(sid, {"segments": [{"utterance_id": f"u{i}", "identity": ident, "name": name,
                                             "t_start": util.iso(st), "t_end": util.iso(en), "text": text, "lang": "en"}]})
        for m in sent[n_sent:]:
            if m.get("type") == "proposal":
                print(f"[{(clock['t'] - t0).total_seconds() / 60:6.1f} min] proposal → {m.get('title')} ({m.get('confidence')}) {m.get('reason')}")
                timeline.append((util.iso(clock["t"]), "PROPOSAL " + str(m.get("title"))))
        n_sent = len(sent)
        live = None
        db = SessionLocal()
        s = db.get(MeetppSession, sid)
        if s.live_section_id:
            live = db.get(MeetppSection, s.live_section_id)
        db.close()
        if live and (not timeline or timeline[-1][1] != live.title):
            timeline.append((util.iso(clock["t"]), live.title))
            print(f"[{(clock['t'] - t0).total_seconds() / 60:6.1f} min] live → {live.title}")
    clock["t"] += timedelta(seconds=tick_s)
    await rt.tick(sid)
    await run_compositions()

    # ── end and finalise ───────────────────────────────────────────────────
    clock["t"] += timedelta(minutes=1)
    await rt.end_session(sid)
    await rt.agent_status(sid, {"status": "offline", "tier2": "off", "final_pass": "skipped", "speakers": []})
    await rt._run_finalisation(sid, retry=False)
    await run_compositions()

    db = SessionLocal()
    s = db.get(MeetppSession, sid)
    data = export.build_export(db, s)
    (out / "export.json").write_text(json.dumps(data, indent=2, default=str))
    from app.meetpp.report import render_meeting_report
    (out / "report.pdf").write_bytes(render_meeting_report(data))
    (out / "minutes.md").write_text(compose.minutes_markdown(db, s))
    decisions = db.query(MeetppDecision).filter_by(session_id=sid).all()
    actions = db.query(MeetppAction).filter_by(series_id=s.series_id).all()
    rejected = db.query(MeetppOp).filter_by(session_id=sid, status="rejected").all()
    reasons: dict[str, int] = {}
    for r in rejected:
        reasons[r.reason or "?"] = reasons.get(r.reason or "?", 0) + 1
    minutes = db.query(MeetppMinute).filter_by(session_id=sid).all()
    summary = {
        "status": s.status, "jobs": json.loads(s.jobs_json or "{}"), "stats": stats,
        "timeline": timeline,
        "decisions": [(d.ref, d.status, d.title) for d in decisions],
        "actions_new": [(a.ref, a.status, a.title) for a in actions if a.session_id == sid],
        "actions_reported": sum(1 for a in actions if a.session_id != sid),
        "rejected_reasons": reasons,
        "minutes": [(m.kind, m.status, len(m.narrative_md or "")) for m in minutes],
        "messages": {t: sum(1 for m in sent if m.get("type") == t) for t in {m.get("type") for m in sent}},
    }
    db.close()
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
