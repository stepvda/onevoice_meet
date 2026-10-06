#!/usr/bin/env python3
"""Meet++ interpretation harness (FDD v3.1 §8, §12).

Replays the stored transcript of a session through the live tick prompt and
the configured LLM in 2-minute windows (with the 90 s context), validates
every returned operation and note with the real validator inside a
transaction that is rolled back, and reports JSON validity (with the one
repair re-prompt), applied / rejected operations with reasons, topics and
token usage. Nothing is written to the database except LLM call records
(and those only with --record).

USAGE
  cd meeting-api
  .venv/bin/python tools/meetpp_eval.py --session <sid> [--limit 40] [--out ops.json] [--record]

It reads the same settings as the app (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL),
so point those at the provider under test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.meetpp import llm, ops, outline, prompts, runtime  # noqa: E402
from app.meetpp.models import MeetppSegment, MeetppSession  # noqa: E402


def _windows(segments: list[MeetppSegment]) -> list[list[MeetppSegment]]:
    out: list[list[MeetppSegment]] = []
    current: list[MeetppSegment] = []
    for s in segments:
        t = runtime._seg_time(s)
        if current and t > runtime._seg_time(current[0]) + timedelta(seconds=runtime.WINDOW_SECONDS):
            out.append(current)
            current = []
        current.append(s)
    if current:
        out.append(current)
    return out


async def run(session_id: str, limit: int, record: bool) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            raise SystemExit(f"session {session_id} not found")
        segments = (
            db.query(MeetppSegment)
            .filter(MeetppSegment.session_id == session_id, MeetppSegment.is_gap.is_(False))
            .order_by(MeetppSegment.seq)
            .all()
        )
    finally:
        db.close()
    if not segments:
        raise SystemExit("no transcript segments for this session")
    if not llm.llm_configured():
        raise SystemExit("LLM_API_KEY is not set")

    stats: Counter = Counter()
    reasons: Counter = Counter()
    results = []
    windows = _windows(segments)[:limit]
    for idx, window in enumerate(windows):
        stats["ticks"] += 1
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            o = outline.load(db, session_id)
            ctx_from = runtime._seg_time(window[0]) - timedelta(seconds=int(settings.meetpp_context_seconds))
            context = [s for s in segments if s.seq < window[0].seq and runtime._seg_time(s) >= ctx_from][-40:]
            messages = prompts.build_tick_messages(**runtime._prompt_data(db, session, o, window, context))
            try:
                parsed, result = await llm.complete_parsed(
                    db=db if record else None, purpose="eval", messages=messages, max_tokens=2000, temperature=0.2,
                    session_id=session_id if record else None,
                )
            except llm.LLMParseError as exc:
                stats["invalid_json"] += 1
                print(f"  window {idx}: invalid output after repair: {exc}")
                continue
            except llm.LLMError as exc:
                stats["errors"] += 1
                print(f"  window {idx}: LLM error: {exc}")
                continue
            stats["valid_json"] += 1
            stats["tokens_in"] += result.prompt_tokens
            stats["tokens_cached"] += result.cached_tokens
            stats["tokens_out"] += result.completion_tokens
            # Validate against the real applier, then roll everything back.
            ctx = ops.ApplyContext(
                db=db, session=session, actor="ai",
                window={s.seq for s in window}, context={s.seq for s in context},
                pins={label: sid for sid, label in o.prompt_ids().items()},
            )
            ops.apply_ops(ctx, parsed.get("ops") or [])
            notes = ops.apply_notes(ctx, parsed.get("notes") or [])
            stats["ops_applied"] += ctx.applied
            stats["ops_suggested"] += ctx.suggested
            stats["ops_rejected"] += len(ctx.rejected) - ctx.suggested
            stats["notes"] += notes
            for r in ctx.rejected:
                reasons[r["reason"].split(":")[0][:60]] += 1
            topic = (parsed.get("topic") or {}) if isinstance(parsed.get("topic"), dict) else {}
            if parsed.get("advance"):
                stats["advances"] += 1
            results.append({"window": [window[0].seq, window[-1].seq], "topic": topic, "advance": parsed.get("advance"),
                            "ops": parsed.get("ops") or [], "notes": parsed.get("notes") or [], "rejected": ctx.rejected})
        finally:
            db.rollback()
            db.close()
    stats["json_validity"] = round(stats["valid_json"] / max(1, stats["ticks"]), 3)
    return {"stats": dict(stats), "rejection_reasons": dict(reasons.most_common()), "ticks": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, help="meetpp session id")
    ap.add_argument("--limit", type=int, default=200, help="max windows")
    ap.add_argument("--out", help="write the per-window output to this JSON file")
    ap.add_argument("--record", action="store_true", help="record the LLM calls in meetpp_llm_calls")
    args = ap.parse_args()
    result = asyncio.run(run(args.session, args.limit, args.record))
    print(json.dumps({"stats": result["stats"], "rejection_reasons": result["rejection_reasons"]}, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"written to {args.out}")


if __name__ == "__main__":
    main()
