"""Session runtime (orchestrator).

One asyncio task per active session inside meeting-api, resumable from the
database. The LLM only propose; validation, application and broadcast are
deterministic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from livekit import api as lk_api
from sqlalchemy import func

from app.config import settings
from app.db import SessionLocal
from app.livekit_client import livekit_api, mint_agent_token
from app.meetpp import llm, ops, phases, prompts, render
from app.meetpp.locales import t
from app.meetpp.models import (
    MeetppAction,
    MeetppAgendaItem,
    MeetppAttendee,
    MeetppConsent,
    MeetppDecision,
    MeetppMinute,
    MeetppOp,
    MeetppSegment,
    MeetppSession,
    MeetppSeries,
    utcnow,
)
from app.models import Meeting

log = logging.getLogger("app.meetpp")

# Wake at least this often to re-check the trigger policy. The ingest event
# also wakes the loop immediately, so ticks run close to per-utterance.
TICK_INTERVAL_SECONDS = 4
# Batch very short fragments, but otherwise process new speech almost
# immediately (the user expects per-sentence extraction).
WORDS_PER_TICK = 60
IDLE_TICK_SECONDS = 3
CONTEXT_OVERLAP_SECONDS = 60
MAX_ANNOUNCE_TITLE = 60
ANNOUNCE_DURATION_DEFAULT_MS = 2500

CUE_RE = re.compile(
    r"\b(action|actiepunt|actie|we agree|afgesproken|decided|besliss|decision|next item|volgend punt|"
    r"any other business|rondvraag|to conclude|afronden|point suivant|prochaine réunion|nächster punkt)\b",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class AgentClient:
    """Thin HTTP client for meetpp-agent's session API."""

    def __init__(self) -> None:
        self.base = settings.meetpp_agent_url.rstrip("/")

    async def start(self, payload: dict) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{self.base}/sessions", json=payload)
                # 409 = the agent already has this session; treat as healthy.
                return r.status_code in (200, 201, 409)
        except Exception as exc:  # noqa: BLE001
            log.warning("meetpp-agent start failed: %s", exc)
            return False

    async def patch(self, sid: str, payload: dict) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.patch(f"{self.base}/sessions/{sid}", json=payload)
                return r.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    async def stop(self, sid: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.delete(f"{self.base}/sessions/{sid}")
                return r.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    async def tts(self, text: str, lang: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{self.base}/tts", json={"text": text, "lang": lang})
                if r.status_code >= 400:
                    return None
                return r.json()
        except Exception:  # noqa: BLE001
            return None

    async def health(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base}/health")
                return r.json() if r.status_code < 400 else None
        except Exception:  # noqa: BLE001
            return None


class SessionRunner:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.event = asyncio.Event()
        self.stop = asyncio.Event()
        self.agent = AgentClient()
        self.task: asyncio.Task | None = None
        self.agent_ok = False
        self.paused = False
        self._lease_held = True
        self._last_nudge_at: datetime | None = None
        self._last_announce_text: str | None = None
        self._last_announce_at: datetime | None = None
        self._room_name: str | None = None
        self._lk = None
        self._last_health_at = 0.0

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        if not await self._acquire_lease():
            log.info("meetpp: session %s already held elsewhere", self.session_id)
            return
        # Setup is best-effort: a transient failure here must not kill the whole
        # session (which would silently stop captions and AI extraction).
        try:
            if not await self._start_agent():
                await self._set_agent_status("offline", None)
            await self._broadcast("session", sid=self.session_id, state="started")
            await self._announce(
                t("en", "announce.start.title"),
                t("en", "announce.start.subtitle"),
            )
            self._touch_metadata(active=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("meetpp: runner setup failed for %s", self.session_id)
        try:
            while not self.stop.is_set():
                try:
                    await self._loop_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("meetpp: loop error (continuing) for %s", self.session_id)
                    await asyncio.sleep(2)
                # Health watchdog: every ~20 s confirm the agent still serves
                # this session; if not, recreate it so transcription resumes.
                now = time.monotonic()
                if now - self._last_health_at > 20:
                    self._last_health_at = now
                    try:
                        health = await self.agent.health()
                        ids = {d.get("session_id") for d in (health or {}).get("sessions_detail", [])}
                        if health is None or self.session_id not in ids:
                            self.agent_ok = False
                    except Exception:  # noqa: BLE001
                        self.agent_ok = False
                if not self.agent_ok and not self.paused:
                    try:
                        self.agent_ok = await self._start_agent()
                    except Exception:  # noqa: BLE001
                        self.agent_ok = False
        except asyncio.CancelledError:
            raise
        finally:
            await self._close_lk()
            self._release_lease()

    async def stop_runner(self) -> None:
        self.stop.set()
        self.event.set()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()

    async def _loop_once(self) -> None:
        try:
            await asyncio.wait_for(self.event.wait(), timeout=TICK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        self.event.clear()
        if self.stop.is_set():
            return
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            if session is None or session.status not in ("running", "paused", "setup"):
                if session is not None and session.status in ("finalising", "review", "published"):
                    self.stop.set()
                return
            self.paused = session.status == "paused"
            if not self.paused:
                await self._maybe_tick(db, session)
            await self._check_proposal(db, session)
            await self._check_timebox(db, session)
        except Exception:  # noqa: BLE001
            log.exception("meetpp: loop error for %s", self.session_id)
        finally:
            db.close()

    # ── agent ──────────────────────────────────────────────────────────
    async def _start_agent(self) -> bool:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            meeting = db.get(Meeting, session.meeting_id)
            identity = f"meetpp-scribe-{session.id}"
            token = mint_agent_token(room_name=meeting.room_name, identity=identity)
            rows = db.query(MeetppConsent).filter_by(session_id=session.id).all()
            opted_out = [c.identity for c in rows if c.decision == "opt_out"]
            accepted = [c.identity for c in rows if c.decision == "accept"]
            vocabulary = _vocabulary(db, session)
            ok = await self.agent.start(
                {
                    "session_id": session.id,
                    "room": meeting.room_name,
                    "ws_url": _internal_ws_url(),
                    "token": token,
                    "language": session.language,
                    "stt_model": settings.stt_model,
                    "accepted": accepted,
                    "opted_out": opted_out,
                    "vocabulary": vocabulary,
                }
            )
            self.agent_ok = ok
            return ok
        finally:
            db.close()

    async def pause_agent(self, paused: bool) -> None:
        await self.agent.patch(self.session_id, {"paused": paused})

    async def set_opt_out(self, identities: list[str]) -> None:
        await self.agent.patch(self.session_id, {"opted_out": identities})

    # ── ticks ──────────────────────────────────────────────────────────
    async def _maybe_tick(self, db, session: MeetppSession) -> None:
        cursor = session.transcript_cursor
        new_segments = (
            db.query(MeetppSegment)
            .filter(MeetppSegment.session_id == session.id, MeetppSegment.seq > cursor)
            .order_by(MeetppSegment.seq)
            .all()
        )
        if not new_segments:
            return
        words = sum(len((s.text or "").split()) for s in new_segments)
        cue = any(CUE_RE.search(s.text or "") for s in new_segments)
        # With no previous tick, treat the session as due immediately so the
        # first utterance is processed (otherwise elapsed is 0 forever until
        # enough words accumulate).
        since_tick = float(IDLE_TICK_SECONDS + 1)
        if session.last_tick_at:
            since_tick = (_now() - _aware(session.last_tick_at)).total_seconds()
        trigger = None
        if cue:
            trigger = "cue"
        elif words >= WORDS_PER_TICK:
            trigger = "words"
        elif since_tick >= IDLE_TICK_SECONDS:
            trigger = "idle"
        if trigger is None:
            return
        await self._run_tick(db, session, new_segments, trigger)

    async def _run_tick(self, db, session: MeetppSession, new_segments: list[MeetppSegment], trigger: str) -> None:
        seqs = [s.seq for s in new_segments]
        window_min, window_max = min(seqs), max(seqs)
        first = new_segments[0]
        overlap_start = _aware(first.t_start) - timedelta(seconds=CONTEXT_OVERLAP_SECONDS) if first.t_start else None
        context = []
        if overlap_start is not None:
            context = (
                db.query(MeetppSegment)
                .filter(
                    MeetppSegment.session_id == session.id,
                    MeetppSegment.seq <= session.transcript_cursor,
                    MeetppSegment.t_end >= overlap_start,
                )
                .order_by(MeetppSegment.seq)
                .all()
            )
        if not llm.llm_configured():
            self._advance_cursor(db, session, seqs)
            return

        aliases = _alias_table(db, session)
        agenda = [
            {
                "id": i.id,
                "pos": i.position,
                "title": i.title,
                "presenter_alias": _alias_for(aliases, i.presenter),
                "timebox": i.timebox_minutes,
                "status": i.status,
            }
            for i in db.query(MeetppAgendaItem).filter_by(session_id=session.id).order_by(MeetppAgendaItem.position).all()
        ]
        prev_actions = [
            {
                "id": a.id,
                "ref": a.ref,
                "title": a.title,
                "owner_alias": _alias_for(aliases, a.owner_name),
                "due": a.due_date,
                "status": a.status,
                "review_status": a.review_status,
            }
            for a in db.query(MeetppAction)
            .filter(MeetppAction.series_id == session.series_id, MeetppAction.session_id != session.id)
            .all()
        ]
        state = _compact_state(db, session)
        messages = prompts.build_tick_messages(
            language=session.language,
            title=(db.get(Meeting, session.meeting_id).display_title if db.get(Meeting, session.meeting_id) else ""),
            date=_now().date().isoformat(),
            template=session.template,
            phase=session.phase,
            current_item_id=session.current_item_id,
            aliases={k: v[0] for k, v in aliases.items()},
            agenda=agenda,
            prev_actions=prev_actions,
            state=state,
            context=[_seg_dict(s) for s in context],
            window=[_seg_dict(s) for s in new_segments],
        )
        try:
            result = await llm.complete_json(
                db=db,
                purpose="tick",
                messages=messages,
                max_tokens=1200,
                temperature=0.2,
                session_id=session.id,
                enforce_budget=True,
            )
            parsed = llm.parse_json(result.text)
        except llm.LLMBudgetExceeded:
            await self._agent_status_message(session, "budget")
            self._advance_cursor(db, session, seqs)
            return
        except llm.LLMError as exc:
            log.warning("meetpp: tick LLM failed: %s", exc)
            await self._set_agent_status("offline", None)
            return

        ctx = ops.ApplyContext(
            db=db,
            session=session,
            actor="ai",
            window_min=window_min,
            window_max=window_max,
            aliases=aliases,
            version=session.state_version + 1,
        )
        ops.apply_ops(ctx, parsed.get("ops") or [])
        # Only bump the state version when an operation actually changed state.
        # Otherwise the client would see version gaps, fall back to a full
        # refetch (which skips the focus hint) and stop auto-switching tabs.
        mutated = any(a.status == "applied" for a in ctx.applied)
        if mutated:
            session.state_version += 1
        session.last_tick_at = _now()
        session.last_tick_seq = max(seqs)
        self._advance_cursor(db, session, seqs)
        db.commit()

        # Broadcast state delta + focus.
        if mutated:
            payload = ops.delta_from_applied(ctx)
            focus = ops.focus_for(ctx)
            await self._broadcast("state", version=session.state_version, changes=payload["changes"], delta=payload["delta"], focus=focus)
        log.info(
            "MEETPP_TICK sid=%s trigger=%s llm_ms=%s tokens_in=%s tokens_out=%s ops_applied=%s ops_rejected=%s version=%s",
            session.id, trigger, result.latency_ms, result.prompt_tokens, result.completion_tokens,
            sum(1 for a in ctx.applied if a.status == "applied"), len(ctx.rejected), session.state_version,
        )
        # Phase signal.
        if ctx.phase_signal:
            proposal = phases.propose(db, session, ctx.phase_signal)
            db.commit()
            if proposal:
                await self._send_proposal(session, proposal)
        # A rule-based proposal can still be due after a tick.
        await self._check_proposal(db, session)

    def _advance_cursor(self, db, session: MeetppSession, seqs: list[int]) -> None:
        session.transcript_cursor = max(seqs)
        db.commit()

    async def _check_proposal(self, db, session: MeetppSession) -> None:
        proposal = phases.current_proposal(session)
        if proposal is None:
            proposal = phases.propose(db, session, None)
            if proposal:
                db.commit()
                await self._send_proposal(session, proposal)
                return
        if not proposal:
            return
        auto_at = proposal.get("auto_at")
        # A "Not yet" suppression window must block auto-accept entirely.
        suppressed = proposal.get("suppressed_until")
        if suppressed:
            try:
                until = datetime.fromisoformat(suppressed)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if _now() < until:
                    return
            except ValueError:
                pass
        if auto_at and session.mode == "lead":
            try:
                deadline = datetime.fromisoformat(auto_at)
            except ValueError:
                deadline = None
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline is not None and _now() >= deadline:
                await self.accept_proposal(db, session, proposal, by="ai")

    async def _check_timebox(self, db, session: MeetppSession) -> None:
        if not _settings_bool(session, "timebox_nudges", True):
            return
        if _now() - (self._last_nudge_at or datetime.min.replace(tzinfo=timezone.utc)) < timedelta(seconds=60):
            return
        ann = phases.timebox_nudge(db, session)
        if ann:
            self._last_nudge_at = _now()
            await self._broadcast("announce", **_announce_payload(session, ann))

    async def accept_proposal(self, db, session: MeetppSession, proposal: dict, by: str) -> None:
        ann = phases.acceptance(db, session, proposal.get("to") or "", proposal.get("item_id"), by)
        db.commit()
        await self._broadcast("phase", phase=session.phase, item_id=session.current_item_id, by=by, at=_now().isoformat())
        await self._broadcast("announce", **_announce_payload(session, ann))
        self._touch_metadata(active=True)
        if session.phase == "closing":
            await self._broadcast("session", sid=session.id, state="ending")

    async def reject_proposal(self, db, session: MeetppSession, proposal: dict | None = None) -> None:
        proposal = proposal or phases.current_proposal(session)
        phases.suppress(db, session, proposal)
        db.commit()

    async def jump_phase(self, db, session: MeetppSession, to: str, item_id: str | None, by: str) -> dict:
        ann = phases.acceptance(db, session, to, item_id, by)
        db.commit()
        await self._broadcast("phase", phase=session.phase, item_id=session.current_item_id, by=by, at=_now().isoformat())
        await self._broadcast("announce", **_announce_payload(session, ann))
        return ann

    async def apply_human_ops(self, session_id: str, raw_ops: list[dict], actor: str) -> dict:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            if session is None:
                return {"applied": [], "version": 0}
            ctx = ops.ApplyContext(
                db=db,
                session=session,
                actor=actor,
                window_min=0,
                window_max=10**9,
                aliases=_alias_table(db, session),
                version=session.state_version + 1,
            )
            ops.apply_ops(ctx, raw_ops)
            mutated = any(a.status == "applied" for a in ctx.applied)
            if mutated:
                session.state_version += 1
            db.commit()
            if mutated:
                payload = ops.delta_from_applied(ctx)
                await self._broadcast(
                    "state",
                    version=session.state_version,
                    changes=payload["changes"],
                    delta=payload["delta"],
                    focus=ops.focus_for(ctx),
                )
            return {
                "applied": [a.__dict__ for a in ctx.applied],
                "rejected": [a.__dict__ for a in ctx.rejected],
                "version": session.state_version,
            }
        finally:
            db.close()

    # ── ingest ─────────────────────────────────────────────────────────
    async def ingest_segments(self, session_id: str, segments: list[dict]) -> int:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            if session is None:
                return 0
            consents = db.query(MeetppConsent).filter_by(session_id=session_id).all()
            # Default-deny: only identities that explicitly accepted are
            # transcribed; opt-out is applied on top.
            accepted = {c.identity for c in consents if c.decision == "accept"}
            opted_out = {c.identity for c in consents if c.decision == "opt_out"}
            max_seq = (
                db.query(func.max(MeetppSegment.seq)).filter(MeetppSegment.session_id == session_id).scalar() or 0
            )
            inserted = 0
            captions = []
            for seg in segments:
                identity = str(seg.get("identity") or "")
                if identity in opted_out or identity not in accepted:
                    continue
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                if _echo_guard(text, self._last_announce_text, self._last_announce_at):
                    continue
                max_seq += 1
                row = MeetppSegment(
                    session_id=session_id,
                    seq=max_seq,
                    identity=identity,
                    name=seg.get("name"),
                    t_start=_parse_dt(seg.get("t_start")),
                    t_end=_parse_dt(seg.get("t_end")),
                    text=text,
                    lang=seg.get("lang") or session.language,
                    avg_logprob=seg.get("avg_logprob"),
                    no_speech_prob=seg.get("no_speech_prob"),
                )
                db.add(row)
                inserted += 1
                duration_ms = None
                if row.t_start and row.t_end:
                    duration_ms = max(0, int((row.t_end - row.t_start).total_seconds() * 1000))
                captions.append(
                    {
                        "seq": max_seq,
                        "identity": identity,
                        "name": seg.get("name") or identity,
                        "text": text,
                        "t": (row.t_end or _now()).isoformat(),
                        "t_start": row.t_start.isoformat() if row.t_start else None,
                        # Utterance duration drives the subtitle scroll speed.
                        "duration_ms": duration_ms,
                    }
                )
            if inserted:
                db.commit()
                await self._broadcast_captions(captions)
                self.event.set()
                await self._set_agent_status("listening", None)
            return inserted
        finally:
            db.close()

    async def apply_presence(self, session_id: str, events: list[dict]) -> None:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            if session is None:
                return
            changed: list = []
            for ev in events:
                identity = str(ev.get("identity") or "")
                name = str(ev.get("name") or identity)
                kind = str(ev.get("kind") or "standard")
                if kind.lower() != "standard":
                    continue
                if identity.startswith(("meetpp-", "playback", "composite-", "viewer-", "egress-", "ingress-")):
                    continue
                person_key = _person_key(identity, name)
                attendee = (
                    db.query(MeetppAttendee)
                    .filter_by(session_id=session_id, person_key=person_key)
                    .first()
                )
                if attendee is None:
                    attendee = MeetppAttendee(
                        id=_ulid(),
                        session_id=session_id,
                        person_key=person_key,
                        display_name=name,
                        identities_json=json.dumps([identity]),
                    )
                    db.add(attendee)
                else:
                    ids = json.loads(attendee.identities_json or "[]")
                    if identity not in ids:
                        ids.append(identity)
                        attendee.identities_json = json.dumps(ids)
                if ev.get("event") == "connected":
                    attendee.presence = "present"
                elif ev.get("event") == "disconnected":
                    attendee.presence = "left"
                # opted-out flag mirrors a consent row
                consent = (
                    db.query(MeetppConsent)
                    .filter_by(session_id=session_id, identity=identity)
                    .first()
                )
                if consent and consent.decision == "opt_out":
                    attendee.opted_out = True
                    attendee.presence = "not_transcribed"
                changed.append(attendee)
            if not changed:
                return
            db.flush()
            session.state_version += 1
            db.commit()
            # Push the changed attendance rows so the tab updates live.
            delta = {"attendance": [ops.attendee_dict(a) for a in changed]}
            await self._broadcast(
                "state",
                version=session.state_version,
                changes=[{"kind": "attendance", "id": a.id, "op": "update"} for a in changed],
                delta=delta,
                focus=None,
            )
            self.event.set()
        finally:
            db.close()

    async def set_consent(self, session_id: str, identity: str, decision: str) -> None:
        db = SessionLocal()
        try:
            row = (
                db.query(MeetppConsent)
                .filter_by(session_id=session_id, identity=identity)
                .first()
            )
            if row is None:
                row = MeetppConsent(session_id=session_id, identity=identity, decision=decision)
                db.add(row)
            else:
                row.decision = decision
                row.updated_at = utcnow()
            # Reflect on attendance.
            changed = []
            for a in db.query(MeetppAttendee).filter_by(session_id=session_id).all():
                ids = json.loads(a.identities_json or "[]")
                if identity in ids:
                    a.opted_out = decision == "opt_out"
                    if decision == "opt_out":
                        a.presence = "not_transcribed"
                    changed.append(a)
            session = db.get(MeetppSession, session_id)
            if changed and session is not None:
                db.flush()
                session.state_version += 1
            db.commit()
            all_rows = db.query(MeetppConsent).filter_by(session_id=session_id).all()
            opted_out = [c.identity for c in all_rows if c.decision == "opt_out"]
            accepted = [c.identity for c in all_rows if c.decision == "accept"]
            await self.agent.patch(
                session_id, {"accepted": accepted, "opted_out": opted_out}
            )
            if changed and session is not None:
                await self._broadcast(
                    "state",
                    version=session.state_version,
                    changes=[{"kind": "attendance", "id": a.id, "op": "update"} for a in changed],
                    delta={"attendance": [ops.attendee_dict(a) for a in changed]},
                    focus=None,
                )
        finally:
            db.close()

    async def pause_session(self, session_id: str, paused: bool) -> None:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            if session is None:
                return
            session.status = "paused" if paused else "running"
            db.commit()
            await self.pause_agent(paused)
            await self._broadcast("session", sid=session_id, state="paused" if paused else "resumed")
            await self._set_agent_status("paused" if paused else "listening", None)
        finally:
            db.close()

    async def announce(self, session_id: str, title: str, subtitle: str = "") -> None:
        await self._bulk_announce(session_id, title, subtitle)

    # ── finalise ───────────────────────────────────────────────────────
    async def finalise(self, session_id: str) -> dict:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            if session is None:
                return {}
            session.status = "finalising"
            db.commit()
            await self._broadcast("session", sid=session_id, state="finalising")
            state = _compact_state(db, session)
            segments = (
                db.query(MeetppSegment)
                .filter_by(session_id=session_id)
                .order_by(MeetppSegment.seq)
                .all()
            )
            transcript = "\n".join(
                f"[{s.seq}] {s.name or s.identity}: {s.text}" for s in segments
            )
            minutes = [
                {"item_id": m.agenda_item_id, "body_md": m.body_md, "locked": m.locked}
                for m in db.query(MeetppMinute).filter_by(session_id=session_id).all()
            ]
            final: dict = {}
            if llm.llm_configured() and transcript:
                try:
                    result = await llm.complete_json(
                        db=db,
                        purpose="finalise",
                        messages=prompts.build_finalise_messages(
                            language=session.language, state=state, transcript=transcript, minutes=minutes
                        ),
                        max_tokens=4000,
                        temperature=0.2,
                        session_id=session_id,
                        enforce_budget=False,
                    )
                    final = llm.parse_json(result.text)
                except llm.LLMError as exc:
                    log.warning("meetpp: finalisation LLM failed: %s", exc)
                    session.error = "Finalisation failed — Retry"
            _apply_final(db, session, final)
            session.final_json = json.dumps(final, ensure_ascii=False)
            session.status = "review"
            session.finalised_at = _now()
            session.ended_at = session.ended_at or _now()
            db.commit()
            await self._broadcast("session", sid=session_id, state="ended")
            await self._stop_agent()
            self._touch_metadata(active=False)
            self.stop.set()
            return final
        finally:
            db.close()

    # ── broadcast / status ─────────────────────────────────────────────
    def _livekit(self):
        """One long-lived LiveKit server client per runner (avoids a new
        connection per broadcast)."""
        if self._lk is None:
            self._lk = livekit_api()
        return self._lk

    async def _close_lk(self) -> None:
        if self._lk is not None:
            try:
                await self._lk.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._lk = None

    async def _room(self) -> str | None:
        if self._room_name:
            return self._room_name
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            if session is not None:
                meeting = db.get(Meeting, session.meeting_id)
                if meeting is not None:
                    self._room_name = meeting.room_name
        finally:
            db.close()
        return self._room_name

    async def _send_raw(self, body: dict) -> None:
        room = await self._room()
        if not room:
            return
        try:
            await self._livekit().room.send_data(
                lk_api.SendDataRequest(
                    room=room,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    kind=lk_api.DataPacket.Kind.RELIABLE,
                    topic="meet-ai",
                )
            )
        except Exception:
            log.exception("meetpp: broadcast %s failed", body.get("type"))

    async def _broadcast(self, mtype: str, **payload) -> None:
        await self._send_raw({"v": 1, "type": mtype, "sid": self.session_id, **payload})

    async def _broadcast_captions(self, captions: list[dict]) -> None:
        """Batch a whole ingest's captions into one data message."""
        if not captions:
            return
        await self._send_raw({"v": 1, "type": "captions", "sid": self.session_id, "items": captions})

    async def _send_proposal(self, session: MeetppSession, proposal: dict) -> None:
        log.info(
            "MEETPP_PROPOSAL sid=%s to=%s source=%s confidence=%s auto_at=%s",
            session.id, proposal.get("to"), proposal.get("source"), proposal.get("confidence"), proposal.get("auto_at"),
        )
        await self._broadcast(
            "proposal",
            pid=proposal.get("pid"),
            to=proposal.get("to"),
            item_id=proposal.get("item_id"),
            reason=proposal.get("reason"),
            confidence=proposal.get("confidence"),
            auto_at=proposal.get("auto_at"),
        )

    async def _announce(self, title: str, subtitle: str) -> None:
        await self._bulk_announce(self.session_id, title, subtitle)

    async def _bulk_announce(self, session_id: str, title: str, subtitle: str) -> None:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            lang = session.language if session else "en"
        finally:
            db.close()
        tts = await self.agent.tts(title, lang)
        audio_url = None
        duration_ms = ANNOUNCE_DURATION_DEFAULT_MS
        if tts and tts.get("hash"):
            ext = tts.get("ext") or "ogg"
            audio_url = f"/api/v1/meetpp/tts/{tts['hash']}.{ext}"
            duration_ms = int(tts.get("duration_ms") or ANNOUNCE_DURATION_DEFAULT_MS)
        self._last_announce_text = title
        self._last_announce_at = _now()
        await self._broadcast(
            "announce",
            aid=f"a{int(_now().timestamp())}",
            title=title[:MAX_ANNOUNCE_TITLE],
            subtitle=subtitle[:90],
            audio_url=audio_url,
            duration_ms=duration_ms,
        )

    async def _set_agent_status(self, status: str, backlog_s: float | None) -> None:
        await self._broadcast("agent", status=status, backlog_s=backlog_s)

    async def _agent_status_message(self, session: MeetppSession, kind: str) -> None:
        await self._broadcast("agent", status=kind, backlog_s=None)

    def _touch_metadata(self, active: bool, board_main: bool = True) -> None:
        asyncio.create_task(self._update_metadata(active, board_main))

    async def _update_metadata(self, active: bool, board_main: bool) -> None:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            if session is None:
                return
            meeting = db.get(Meeting, session.meeting_id)
            lk = livekit_api()
            try:
                rooms = await lk.room.list_rooms(lk_api.ListRoomsRequest(names=[meeting.room_name]))
                current: dict = {}
                if rooms.rooms:
                    try:
                        current = json.loads(rooms.rooms[0].metadata or "{}")
                    except ValueError:
                        current = {}
                current["meetpp"] = {
                    "sid": session.id,
                    "active": active,
                    "phase": session.phase,
                    "board_main": board_main,
                    "public": _settings_bool(session, "show_public", False),
                    "in_recordings": _settings_bool(session, "in_recordings", True),
                }
                await lk.room.update_room_metadata(
                    lk_api.UpdateRoomMetadataRequest(room=meeting.room_name, metadata=json.dumps(current))
                )
            except Exception:
                log.exception("meetpp: metadata update failed")
            finally:
                await lk.aclose()
        finally:
            db.close()

    async def _stop_agent(self) -> None:
        await self.agent.stop(self.session_id)

    # ── lease ──────────────────────────────────────────────────────────
    async def _acquire_lease(self) -> bool:
        try:
            import redis as redis_sync

            def _do():
                r = redis_sync.Redis.from_url(settings.redis_url, socket_timeout=2)
                return bool(r.set(f"meetpp:lease:{self.session_id}", "1", nx=True, ex=30))
            return await asyncio.to_thread(_do)
        except Exception:  # noqa: BLE001
            return True

    def _release_lease(self) -> None:
        self._lease_held = False
        try:
            import redis as redis_sync

            r = redis_sync.Redis.from_url(settings.redis_url, socket_timeout=2)
            r.delete(f"meetpp:lease:{self.session_id}")
        except Exception:  # noqa: BLE001
            pass


class MeetppRuntime:
    def __init__(self) -> None:
        self.runners: dict[str, SessionRunner] = {}

    def get(self, session_id: str) -> SessionRunner:
        runner = self.runners.get(session_id)
        if runner is None:
            runner = SessionRunner(session_id)
            self.runners[session_id] = runner
        return runner

    async def start(self) -> None:
        """Resume every session that was running when the API restarted."""
        if not settings.meetpp_enabled:
            return
        db = SessionLocal()
        try:
            active = (
                db.query(MeetppSession.id)
                .filter(MeetppSession.status.in_(("running", "paused", "setup")))
                .all()
            )
            ids = [r[0] for r in active]
        finally:
            db.close()
        if ids:
            # Clear any stale single-runner leases left by a previous process
            # so a fast restart resumes sessions instead of silently skipping
            # them (the lease has a 30 s TTL).
            try:
                import redis as redis_sync

                def _clear():
                    r = redis_sync.Redis.from_url(settings.redis_url, socket_timeout=2)
                    for sid in ids:
                        r.delete(f"meetpp:lease:{sid}")
                await asyncio.to_thread(_clear)
            except Exception:  # noqa: BLE001
                pass
        for sid in ids:
            self.get(sid).start()
        if ids:
            log.info("meetpp: resumed %d session(s)", len(ids))

    async def stop(self) -> None:
        for runner in list(self.runners.values()):
            await runner.stop_runner()
        self.runners.clear()

    def start_session(self, session_id: str) -> None:
        self.get(session_id).start()

    async def end_session(self, session_id: str) -> dict:
        runner = self.get(session_id)
        result = await runner.finalise(session_id)
        self.runners.pop(session_id, None)
        return result


runtime = MeetppRuntime()


# ─── helpers ────────────────────────────────────────────────────────────────


def _internal_ws_url() -> str:
    # The agent joins over the Docker network, like egress.
    return "ws://host.docker.internal:7880"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _ulid() -> str:
    from ulid import ULID

    return str(ULID())


def _person_key(identity: str, name: str) -> str:
    from app.meetpp.util import person_key

    return person_key(identity, name)


def _alias_table(db, session: MeetppSession) -> dict[str, tuple[str, str | None]]:
    # When aliasing is disabled the LLM sees real names (and returns them); the
    # validator matches owners by name.
    if not settings.meetpp_speaker_aliasing:
        return {}
    rows = db.query(MeetppAttendee).filter_by(session_id=session.id).order_by(MeetppAttendee.created_at).all()
    table: dict[str, tuple[str, str | None]] = {}
    for i, a in enumerate(rows, start=1):
        table[f"P{i}"] = (a.display_name, a.person_key)
    return table


def _alias_for(table: dict[str, tuple[str, str | None]], name: str | None) -> str | None:
    if not name:
        return None
    for alias, (display, _key) in table.items():
        if display.lower() == name.lower():
            return alias
    return None


def _seg_dict(s: MeetppSegment) -> dict:
    return {
        "seq": s.seq,
        "speaker": s.name or s.identity,
        "t": (_aware(s.t_start).strftime("%H:%M:%S") if s.t_start else ""),
        "text": s.text,
    }


def _compact_state(db, session: MeetppSession) -> dict:
    decisions = [
        {"id": d.id, "ref": d.ref, "text": d.text[:200], "status": d.status, "locked": d.locked}
        for d in db.query(MeetppDecision).filter_by(session_id=session.id).all()
    ]
    actions = [
        {"id": a.id, "ref": a.ref, "title": a.title[:120], "owner": a.owner_name, "due": a.due_date, "status": a.status, "locked": a.locked}
        for a in db.query(MeetppAction).filter(MeetppAction.series_id == session.series_id).all()
    ]
    minutes = []
    for m in db.query(MeetppMinute).filter_by(session_id=session.id).all():
        active = m.agenda_item_id == session.current_item_id
        minutes.append({"item_id": m.agenda_item_id, "body": m.body_md if active else m.body_md[:300], "locked": m.locked})
    locked = [d["id"] for d in decisions if d["locked"]] + [a["id"] for a in actions if a["locked"]] + [
        m["item_id"] for m in minutes if m["locked"]
    ]
    return {"decisions": decisions, "actions": actions, "minutes": minutes, "locked_ids": locked}


def _vocabulary(db, session: MeetppSession) -> list[str]:
    words: list[str] = []
    for i in db.query(MeetppAgendaItem).filter_by(session_id=session.id).all():
        words.append(i.title)
        if i.presenter:
            words.append(i.presenter)
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        words.append(a.display_name)
    return words[:40]


def _echo_guard(text: str, last_text: str | None, last_at: datetime | None) -> bool:
    if not last_text or last_at is None:
        return False
    if _now() - last_at > timedelta(seconds=10):
        return False
    a = set(re.findall(r"\w+", text.lower()))
    b = set(re.findall(r"\w+", last_text.lower()))
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.6


def _announce_payload(session: MeetppSession, ann: dict) -> dict:
    return {
        "aid": f"a{int(_now().timestamp())}",
        "title": (ann.get("title") or "")[:MAX_ANNOUNCE_TITLE],
        "subtitle": (ann.get("subtitle") or "")[:90],
        "audio_url": None,
        "duration_ms": ANNOUNCE_DURATION_DEFAULT_MS,
    }


def _settings_bool(session: MeetppSession, key: str, default: bool) -> bool:
    try:
        data = json.loads(session.settings_json or "{}")
    except ValueError:
        data = {}
    return bool(data.get(key, default))


def _apply_final(db, session: MeetppSession, final: dict) -> None:
    """Apply non-locked finalisation output. Locked items are never changed;
    differences are surfaced in `final.changes`."""
    for m in final.get("minutes") or []:
        item_id = m.get("item_id")
        body = m.get("body_md")
        # `item_id` may be null when there are no agenda items; still store it
        # as a general minutes block so the PDF is never empty.
        if not body:
            continue
        minute = (
            db.query(MeetppMinute)
            .filter_by(session_id=session.id, agenda_item_id=item_id)
            .first()
        )
        if minute is None:
            db.add(MeetppMinute(id=_ulid(), session_id=session.id, agenda_item_id=item_id, body_md=body, origin="ai"))
        elif not minute.locked:
            minute.body_md = body
            minute.version += 1
    existing_decision_texts = {
        row.text.strip().lower()
        for row in db.query(MeetppDecision).filter_by(series_id=session.series_id).all()
    }
    for d in final.get("decisions") or []:
        text = (d.get("text") or "").strip()
        if d.get("id"):
            row = db.get(MeetppDecision, d["id"])
            if row is not None and not row.locked and text:
                row.text = text[:600]
                existing_decision_texts.add(text.lower())
            continue
        # Finalisation may surface decisions the live ticks missed: add them.
        if not text or text.lower() in existing_decision_texts:
            continue
        item_id = d.get("item_id") or session.current_item_id
        series = db.get(MeetppSeries, session.series_id)
        series.decision_counter += 1
        db.add(
            MeetppDecision(
                id=_ulid(),
                session_id=session.id,
                series_id=session.series_id,
                ref=f"D-{series.decision_counter:02d}",
                agenda_item_id=item_id,
                text=text[:600],
                status="confirmed",
                origin="ai",
            )
        )
        existing_decision_texts.add(text.lower())
    for a in final.get("actions") or []:
        if not a.get("id"):
            continue
        row = db.get(MeetppAction, a["id"])
        if row is not None and not row.locked:
            if a.get("title"):
                row.title = a["title"][:300]
            if a.get("owner"):
                row.owner_name = a["owner"][:200]
            if a.get("due"):
                row.due_date = a["due"][:20]
    for a in final.get("actions") or []:
        if a.get("id"):
            continue
        title = a.get("title")
        if not title:
            continue
        series = db.get(MeetppSeries, session.series_id)
        series.action_counter += 1
        db.add(
            MeetppAction(
                id=_ulid(),
                series_id=session.series_id,
                session_id=session.id,
                ref=f"A-{series.action_counter:02d}",
                title=title[:300],
                owner_name=(a.get("owner") or None),
                due_date=(a.get("due") or None),
                status="open",
                origin="ai",
            )
        )
    for r in final.get("required_next") or []:
        name = (r or {}).get("name")
        if not name:
            continue
        attendee = (
            db.query(MeetppAttendee)
            .filter_by(session_id=session.id, person_key=_person_key(name, name))
            .first()
        )
        if attendee is None:
            attendee = MeetppAttendee(
                id=_ulid(),
                session_id=session.id,
                person_key=_person_key(name, name),
                display_name=name[:200],
                presence="absent",
            )
            db.add(attendee)
        attendee.required_next = True
        attendee.required_reason = (r.get("reason") or "")[:300] or attendee.required_reason
