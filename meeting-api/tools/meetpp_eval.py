#!/usr/bin/env python3
"""Meet++ AI quality harness (FDD §16.3).

Replays the stored transcript of a finished session through the live tick
prompt + LLM provider in windows, and reports JSON validity, operation counts,
rejections and token usage. Use it to compare providers/models or to check a
prompt change against recorded meetings before shipping it.

USAGE
  cd meeting-api
  .venv/bin/python tools/meetpp_eval.py --session <sid> [--window 30] [--limit 40]

It reads the same settings as the app (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL),
so point those at the provider under test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.meetpp import llm, prompts  # noqa: E402
from app.meetpp.models import MeetppAgendaItem, MeetppAttendee, MeetppSegment, MeetppSession  # noqa: E402


async def run(session_id: str, window: int, limit: int) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            raise SystemExit(f"session {session_id} not found")
        segments = (
            db.query(MeetppSegment).filter_by(session_id=session_id).order_by(MeetppSegment.seq).all()
        )
        if not segments:
            raise SystemExit("no transcript segments for this session")
        agenda = [
            {"id": i.id, "pos": i.position, "title": i.title, "status": i.status}
            for i in db.query(MeetppAgendaItem).filter_by(session_id=session_id).all()
        ]
        aliases = {
            f"P{n}": a.display_name
            for n, a in enumerate(db.query(MeetppAttendee).filter_by(session_id=session_id).all(), start=1)
        }
    finally:
        db.close()

    stats = {"ticks": 0, "valid_json": 0, "ops": 0, "phase_signals": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}
    all_ops: list[dict] = []
    windows = [segments[i : i + window] for i in range(0, min(len(segments), limit * window), window)]
    for idx, win in enumerate(windows):
        stats["ticks"] += 1
        messages = prompts.build_tick_messages(
            language=session.language,
            title="eval",
            date="2026-01-01",
            template=session.template,
            phase=session.phase,
            current_item_id=session.current_item_id,
            aliases=aliases,
            agenda=agenda,
            prev_actions=[],
            state={"decisions": [], "actions": [], "minutes": [], "locked_ids": []},
            context=[],
            window=[{"seq": s.seq, "speaker": s.name or s.identity, "t": "", "text": s.text} for s in win],
        )
        try:
            result = await llm.complete_json(
                db=None, purpose="eval", messages=messages, max_tokens=1200, temperature=0.2, enforce_budget=False
            )
            parsed = llm.parse_json(result.text)
            stats["valid_json"] += 1
            stats["tokens_in"] += result.prompt_tokens
            stats["tokens_out"] += result.completion_tokens
            for op in parsed.get("ops") or []:
                stats["ops"] += 1
                all_ops.append(op)
            if parsed.get("phase_signal"):
                stats["phase_signals"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"  window {idx}: ERROR {exc}")
    stats["json_validity"] = round(stats["valid_json"] / max(1, stats["ticks"]), 3)
    return {"stats": stats, "ops": all_ops}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, help="meetpp session id")
    ap.add_argument("--window", type=int, default=30, help="segments per tick")
    ap.add_argument("--limit", type=int, default=100, help="max windows")
    ap.add_argument("--out", help="write the raw ops JSON to this file")
    args = ap.parse_args()
    result = asyncio.run(run(args.session, args.window, args.limit))
    print(json.dumps(result["stats"], indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"ops written to {args.out}")


if __name__ == "__main__":
    main()
