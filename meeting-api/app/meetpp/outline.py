"""Meeting outline: fixed sections + agenda points, numbering and the live
position (FDD §4.2, §5.3, §8.3; contract §1).

Positions: top-level sections are ordered by `position`; sub-points
(`parent_id` set) are ordered by `position` within their parent. Outline
order = each top-level section followed by its sub-points. Navigation
(Next/Back) walks top-level sections only and passes over skipped ones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.meetpp import util
from app.meetpp.models import (
    MeetppAction,
    MeetppAttachment,
    MeetppDecision,
    MeetppMinute,
    MeetppSection,
    MeetppSession,
)

AOB_RE = re.compile(r"any other (urgent )?(business|matters?)|\baob\b|a\.o\.b", re.IGNORECASE)
# An agenda point that reviews the previous actions ("Actions from 30 September",
# "Review of open actions", "Matters arising"): it becomes their home and the
# fixed "Previous actions" section is skipped, so they are listed once.
ACTIONS_POINT_RE = re.compile(
    r"^\s*(previous|open|outstanding|pending|follow[- ]?up)\s+actions?\b"
    r"|^\s*actions?\s+(from|of|since|review|list|arising|follow[- ]?up|status|update)\b"
    r"|^\s*(review|status|update)\s+(of\s+)?(the\s+)?(previous\s+|open\s+|outstanding\s+)?actions?\b"
    r"|^\s*action\s+(items?|list|points?|log)\b"
    r"|^\s*matters\s+arising\b",
    re.IGNORECASE,
)

FIXED_TITLES = {
    "opening": "Opening",
    "previous_actions": "Previous actions",
    "aob": "Any other business",
    "new_actions": "New actions",
    "closing": "Closing",
    "goal": "Goal",
    "deliverables": "Deliverables",
    "next_steps": "Next steps",
    "planning": "Planning",
}

TEMPLATES = {
    "agenda": ["opening", "previous_actions", "aob", "new_actions", "closing"],
    "goal": ["opening", "goal", "deliverables", "next_steps", "planning", "closing"],
}
# Fixed kinds that come after the agenda points (agenda points are inserted
# before the first of these).
AFTER_AGENDA = ("aob", "new_actions", "deliverables", "next_steps", "planning", "closing")

MAX_SECTIONS = 120
AI_UNDO_SECONDS = 10
CHAIR_SUPPRESS_SECONDS = 60
OPEN_ACTION_STATUSES = ("proposed", "open", "in_progress")


class OutlineError(ValueError):
    pass


@dataclass
class Outline:
    flat: list[MeetppSection]
    by_id: dict[str, MeetppSection]
    children: dict[str, list[MeetppSection]]
    numbers: dict[str, str | None]
    index: dict[str, int]
    # Prompt ids ("S5" → section id) pinned to the outline the LLM saw, so a
    # section added meanwhile cannot shift the numbering of a reply.
    pins: dict[str, str] | None = None

    def top(self, s: MeetppSection) -> MeetppSection:
        if s.parent_id and s.parent_id in self.by_id:
            return self.by_id[s.parent_id]
        return s

    def tops(self) -> list[MeetppSection]:
        return [s for s in self.flat if not s.parent_id]

    def nav(self) -> list[MeetppSection]:
        return [s for s in self.tops() if s.status != "skipped"]

    def prompt_ids(self) -> dict[str, str]:
        return {s.id: f"S{i}" for i, s in enumerate(self.flat, start=1)}

    def resolve(self, ref) -> MeetppSection | None:
        """Section from a prompt id ("S5"), a raw section id or a number ("3.1")."""
        if ref is None:
            return None
        key = str(ref).strip()
        if not key:
            return None
        m = re.fullmatch(r"[Ss]\s*(\d+)", key)
        if m:
            i = int(m.group(1))
            if self.pins is not None:
                pinned = self.pins.get(f"S{i}")
                return self.by_id.get(pinned) if pinned else None
            return self.flat[i - 1] if 1 <= i <= len(self.flat) else None
        if key in self.by_id:
            return self.by_id[key]
        for sid, num in self.numbers.items():
            if num and num == key:
                return self.by_id[sid]
        return None

    def later(self, a: MeetppSection, b: MeetppSection) -> bool:
        """True when `a` comes after `b` in outline order."""
        return self.index.get(a.id, -1) > self.index.get(b.id, -1)


def load(db: Session, session_id: str) -> Outline:
    rows = db.query(MeetppSection).filter_by(session_id=session_id).all()
    by_id = {s.id: s for s in rows}
    tops = sorted([s for s in rows if not s.parent_id or s.parent_id not in by_id], key=lambda s: (s.position, s.created_at))
    children: dict[str, list[MeetppSection]] = {}
    for s in rows:
        if s.parent_id and s.parent_id in by_id:
            children.setdefault(s.parent_id, []).append(s)
    for lst in children.values():
        lst.sort(key=lambda s: (s.position, s.created_at))
    flat: list[MeetppSection] = []
    for t in tops:
        flat.append(t)
        flat.extend(children.get(t.id, []))
    numbers: dict[str, str | None] = {s.id: None for s in rows}
    n = 0
    for t in tops:
        if t.kind == "agenda":
            n += 1
            numbers[t.id] = str(n)
            for m, c in enumerate(children.get(t.id, []), start=1):
                numbers[c.id] = f"{n}.{m}"
    index = {s.id: i for i, s in enumerate(flat)}
    return Outline(flat=flat, by_id=by_id, children=children, numbers=numbers, index=index)


def label(outline: Outline, s: MeetppSection) -> str:
    num = outline.numbers.get(s.id)
    return f"{num} · {s.title}" if num else s.title


def _renumber(outline_rows: list[MeetppSection], children: dict[str, list[MeetppSection]]) -> None:
    for i, t in enumerate(outline_rows, start=1):
        t.position = i * 10
        for j, c in enumerate(children.get(t.id, []), start=1):
            c.position = j * 10


def create_fixed_sections(db: Session, session: MeetppSession) -> list[MeetppSection]:
    kinds = TEMPLATES.get(session.template or "agenda", TEMPLATES["agenda"])
    out = []
    for i, kind in enumerate(kinds, start=1):
        s = MeetppSection(
            id=util.ulid(),
            session_id=session.id,
            kind=kind,
            position=i * 10,
            title=FIXED_TITLES[kind],
            status="pending",
            source="template",
        )
        db.add(s)
        out.append(s)
    db.flush()
    apply_skip_rules(db, session)
    return out


def actions_point(o: "Outline") -> MeetppSection | None:
    """The agenda point that reviews the previous actions, if the agenda has one."""
    for s in o.tops():
        if s.kind == "agenda" and ACTIONS_POINT_RE.search(s.title or ""):
            return s
    return None


def previous_actions_home(o: "Outline") -> MeetppSection | None:
    """Where the previous actions are listed, reported on and minuted: the
    agenda's own actions point, else the fixed "Previous actions" section."""
    point = actions_point(o)
    if point is not None:
        return point
    return next((s for s in o.tops() if s.kind == "previous_actions"), None)


def has_open_previous_actions(db: Session, session: MeetppSession) -> bool:
    return (
        db.query(MeetppAction.id)
        .filter(
            MeetppAction.series_id == session.series_id,
            MeetppAction.session_id != session.id,
            MeetppAction.status.in_(OPEN_ACTION_STATUSES),
        )
        .first()
        is not None
    ) or (
        # Actions imported from a previous-notes PDF are raised "in" this
        # session (origin pdf) but are previous actions all the same.
        db.query(MeetppAction.id)
        .filter(
            MeetppAction.session_id == session.id,
            MeetppAction.origin == "pdf",
            MeetppAction.status.in_(OPEN_ACTION_STATUSES),
        )
        .first()
        is not None
    )


def apply_skip_rules(db: Session, session: MeetppSession) -> list[str]:
    """Skip `previous_actions` without open series actions or when an agenda
    point reviews them, and `aob` when the last agenda point is an AOB point.
    Only pending/skipped rows change."""
    db.flush()
    o = load(db, session.id)
    changed: list[str] = []
    agenda_tops = [s for s in o.tops() if s.kind == "agenda"]
    last_is_aob = bool(agenda_tops) and bool(AOB_RE.search(agenda_tops[-1].title or ""))
    want = {
        "previous_actions": actions_point(o) is not None or not has_open_previous_actions(db, session),
        "aob": last_is_aob,
    }
    for s in o.tops():
        if s.kind not in want or s.status not in ("pending", "skipped"):
            continue
        new = "skipped" if want[s.kind] else "pending"
        if s.status != new:
            s.status = new
            changed.append(s.id)
    return changed


def _clean_int(value, lo: int = 0, hi: int = 600) -> int | None:
    if value in (None, ""):
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def replace_agenda(db: Session, session: MeetppSession, points: list[dict], source: str = "user") -> set[str]:
    """Replace the agenda points (and their sub-points). Fixed sections are
    untouched. Returns the ids of every section that changed or was removed."""
    o = load(db, session.id)
    changed: set[str] = set()
    keep: set[str] = set()
    new_tops: list[MeetppSection] = []
    new_children: dict[str, list[MeetppSection]] = {}
    for p in points:
        title = util.truncate(p.get("title"), 400)
        if not title:
            continue
        sec = o.by_id.get(str(p.get("id") or ""))
        if sec is None or sec.kind != "agenda" or sec.parent_id:
            sec = MeetppSection(id=util.ulid(), session_id=session.id, kind="agenda", position=0, title=title, status="pending", source=source)
            db.add(sec)
        sec.title = title
        sec.body = util.truncate(p.get("body"), 4000)
        sec.presenter = util.truncate(p.get("presenter"), 200)
        sec.timebox_minutes = _clean_int(p.get("timebox_minutes"))
        keep.add(sec.id)
        changed.add(sec.id)
        new_tops.append(sec)
        kids = []
        for sp in p.get("subpoints") or []:
            if not isinstance(sp, dict):
                continue
            st = util.truncate(sp.get("title"), 400)
            if not st:
                continue
            child = o.by_id.get(str(sp.get("id") or ""))
            if child is None or child.kind != "agenda" or not child.parent_id:
                child = MeetppSection(id=util.ulid(), session_id=session.id, kind="agenda", position=0, title=st, status="pending", source=source)
                db.add(child)
            child.parent_id = sec.id
            child.title = st
            child.body = util.truncate(sp.get("body"), 4000)
            keep.add(child.id)
            changed.add(child.id)
            kids.append(child)
        new_children[sec.id] = kids
    # Removed agenda rows (top-level agenda points and sub-points of kept or
    # removed agenda points). AOB items raised under the fixed AOB section stay.
    removed: list[MeetppSection] = []
    for s in o.flat:
        if s.id in keep:
            continue
        parent = o.by_id.get(s.parent_id) if s.parent_id else None
        is_agenda_top = s.kind == "agenda" and not s.parent_id
        is_agenda_child = bool(parent) and parent.kind == "agenda"
        if is_agenda_top or is_agenda_child:
            removed.append(s)
    for s in removed:
        if s.id == session.live_section_id:
            raise OutlineError("the live section cannot be removed")
    db.flush()
    for s in removed:
        fallback = None
        if s.parent_id and s.parent_id in keep:
            fallback = s.parent_id
        else:
            fallback = session.live_section_id if session.live_section_id not in {r.id for r in removed} else None
        _reassign_section_content(db, session, s.id, fallback)
        db.delete(s)
        changed.add(s.id)
    db.flush()
    # Order: fixed before the agenda, agenda points, fixed after.
    fixed_tops = [s for s in o.tops() if s.kind != "agenda"]
    before = []
    after = []
    seen_after = False
    for s in fixed_tops:
        if s.kind in AFTER_AGENDA:
            seen_after = True
        (after if seen_after else before).append(s)
    if session.template == "goal":
        # Goal template: agenda points (if any) follow the "Goal" section.
        before = [s for s in fixed_tops if s.kind in ("opening", "goal")]
        after = [s for s in fixed_tops if s.kind not in ("opening", "goal")]
    ordered = before + new_tops + after
    children = {**{k: v for k, v in o.children.items() if k not in new_children}, **new_children}
    _renumber(ordered, children)
    db.flush()
    changed.update(apply_skip_rules(db, session))
    return changed


def _reassign_section_content(db: Session, session: MeetppSession, section_id: str, fallback: str | None) -> None:
    # Decisions still to take that were prefilled for a removed point go with it.
    db.query(MeetppDecision).filter(
        MeetppDecision.session_id == session.id,
        MeetppDecision.section_id == section_id,
        MeetppDecision.status == "pending",
        MeetppDecision.locked.is_(False),
    ).delete(synchronize_session=False)
    for model in (MeetppDecision, MeetppAttachment):
        db.query(model).filter(model.session_id == session.id, model.section_id == section_id).update(
            {model.section_id: fallback}, synchronize_session=False
        )
    db.query(MeetppAction).filter(MeetppAction.session_id == session.id, MeetppAction.section_id == section_id).update(
        {MeetppAction.section_id: fallback}, synchronize_session=False
    )
    minute = db.query(MeetppMinute).filter_by(session_id=session.id, kind="section", section_id=section_id).first()
    if minute is None:
        return
    notes = util.loads(minute.notes_json, [])
    if notes and fallback:
        target = minute_for(db, session, fallback)
        merged = util.loads(target.notes_json, []) + notes
        target.notes_json = util.dumps(merged)
    db.delete(minute)


def minute_for(db: Session, session: MeetppSession, section_id: str | None, kind: str = "section") -> MeetppMinute:
    q = db.query(MeetppMinute).filter_by(session_id=session.id, kind=kind)
    q = q.filter(MeetppMinute.section_id == section_id) if section_id else q.filter(MeetppMinute.section_id.is_(None))
    m = q.first()
    if m is None:
        m = MeetppMinute(
            id=util.ulid(),
            session_id=session.id,
            kind=kind,
            section_id=section_id,
            notes_json="[]",
            status="notes",
            version=0,
        )
        db.add(m)
        db.flush()
    return m


def add_section(
    db: Session,
    session: MeetppSession,
    *,
    title: str,
    kind: str = "agenda",
    parent_id: str | None = None,
    source: str = "user",
) -> MeetppSection:
    """Add an agenda point, a sub-point (parent given) or an AOB item."""
    o = load(db, session.id)
    if len(o.flat) >= MAX_SECTIONS:
        raise OutlineError("outline is full")
    title = util.truncate(title, 400)
    if not title:
        raise OutlineError("title required")
    parent = o.by_id.get(parent_id) if parent_id else None
    if parent_id and parent is None:
        raise OutlineError("parent section not found")
    if parent is not None:
        parent = o.top(parent)
    if kind == "aob" and parent is None:
        aob = next((s for s in o.tops() if s.kind == "aob" and s.status != "skipped"), None)
        agenda_tops = [s for s in o.tops() if s.kind == "agenda"]
        if aob is not None:
            parent = aob
        elif agenda_tops and AOB_RE.search(agenda_tops[-1].title or ""):
            parent = agenda_tops[-1]
    if parent is not None:
        siblings = o.children.get(parent.id, [])
        for sib in siblings:
            if util.norm_name(sib.title) == util.norm_name(title) or util.jaccard(sib.title, title) >= 0.85:
                raise OutlineError("duplicate sub-point")
        sec = MeetppSection(
            id=util.ulid(),
            session_id=session.id,
            kind="agenda",
            parent_id=parent.id,
            position=(siblings[-1].position + 10) if siblings else 10,
            title=title,
            status="pending",
            source=source,
        )
        db.add(sec)
        db.flush()
        return sec
    # New top-level agenda point after the last agenda point.
    tops = o.tops()
    for t in tops:
        if t.kind == "agenda" and (util.norm_name(t.title) == util.norm_name(title) or util.jaccard(t.title, title) >= 0.85):
            raise OutlineError("duplicate agenda point")
    sec = MeetppSection(id=util.ulid(), session_id=session.id, kind="agenda", position=0, title=title, status="pending", source=source)
    db.add(sec)
    agenda_idx = [i for i, t in enumerate(tops) if t.kind == "agenda"]
    if agenda_idx:
        insert_at = agenda_idx[-1] + 1
    else:
        insert_at = next((i for i, t in enumerate(tops) if t.kind in AFTER_AGENDA), len(tops))
    ordered = tops[:insert_at] + [sec] + tops[insert_at:]
    _renumber(ordered, o.children)
    db.flush()
    apply_skip_rules(db, session)
    return sec


# ─── Live position ─────────────────────────────────────────────────────────


@dataclass
class MoveResult:
    prev_id: str | None
    live_id: str
    by: str
    closed: list[str] = field(default_factory=list)  # top-level ids to compose
    changed: set[str] = field(default_factory=set)  # section ids
    minutes_changed: list[str] = field(default_factory=list)
    undo_until: str | None = None


def _topic_state(session: MeetppSession) -> dict:
    return util.loads(session.topic_state_json, {})


def _set_topic_state(session: MeetppSession, state: dict) -> None:
    session.topic_state_json = util.dumps(state)


def live_elapsed(session: MeetppSession, s: MeetppSection) -> float:
    """Total live time: finished runs plus the current run. (SectionDto
    reports the finished runs only; `started_at` is the start of the current
    live run, null while the session is paused.)"""
    total = float(s.elapsed_seconds or 0.0)
    since = util.aware(s.started_at)
    if s.status == "live" and since is not None:
        total += max(0.0, (util.now() - since).total_seconds())
    return round(total, 1)


def _stop_timer(session: MeetppSession, s: MeetppSection) -> None:
    """End the current live run of `s` and add it to the accumulated time."""
    now = util.now()
    since = util.aware(s.started_at)
    if s.status == "live" and since is not None:
        s.elapsed_seconds = float(s.elapsed_seconds or 0.0) + max(0.0, (now - since).total_seconds())
    s.ended_at = now


def pause_timer(session: MeetppSession, s: MeetppSection) -> None:
    _stop_timer(session, s)
    s.started_at = None
    s.ended_at = None


def resume_timer(session: MeetppSession, s: MeetppSection) -> None:
    if s.status == "live":
        s.started_at = util.now()
        s.ended_at = None


def _reopen_minutes(db: Session, session: MeetppSession, section_id: str) -> str | None:
    m = (
        db.query(MeetppMinute)
        .filter_by(session_id=session.id, kind="section", section_id=section_id)
        .first()
    )
    if m is None or m.locked:
        return None
    if m.status in ("composed", "failed", "composing"):
        m.status = "notes"
        return m.id
    return None


def first_section(o: Outline) -> MeetppSection | None:
    nav = o.nav()
    return nav[0] if nav else (o.flat[0] if o.flat else None)


def next_target(o: Outline, session: MeetppSession) -> MeetppSection | None:
    cur = o.by_id.get(session.live_section_id or "")
    nav = o.nav()
    if cur is None:
        return nav[0] if nav else None
    top = o.top(cur)
    for s in nav:
        if o.index[s.id] > o.index[top.id]:
            return s
    return None


def prev_target(o: Outline, session: MeetppSession) -> MeetppSection | None:
    cur = o.by_id.get(session.live_section_id or "")
    if cur is None:
        return None
    top = o.top(cur)
    if cur.parent_id:
        # Back from a sub-point returns to its point.
        return top
    prev = None
    for s in o.nav():
        if o.index[s.id] >= o.index[top.id]:
            break
        prev = s
    return prev


def start_live(db: Session, session: MeetppSession) -> MoveResult | None:
    """Make the first section live when the session starts (or resumes with
    no live section)."""
    o = load(db, session.id)
    if session.live_section_id and session.live_section_id in o.by_id:
        resume_timer(session, o.by_id[session.live_section_id])
        return None
    target = first_section(o)
    if target is None:
        return None
    now = util.now()
    target.status = "live"
    target.started_at = now
    target.ended_at = None
    session.live_section_id = target.id
    session.topic_section_id = target.id
    st = _topic_state(session)
    st.update({"candidate": None, "later": None, "later_hist": [], "later_run": None, "hint": None})
    _set_topic_state(session, st)
    return MoveResult(prev_id=None, live_id=target.id, by="chair", changed={target.id})


def move(
    db: Session,
    session: MeetppSession,
    target: MeetppSection,
    *,
    by: str,
    close_status: str = "done",
) -> MoveResult:
    """Move the live position. `by` is "chair", "ai" or "undo".

    Leaving a section forward closes it (done/deferred, timer stopped) and
    returns the top-level section to compose; leaving it backward returns it
    to pending. Re-entering a closed section reopens it and returns its
    composed narrative to draft (unless locked).
    """
    o = load(db, session.id)
    if target.id not in o.by_id:
        raise OutlineError("section not found")
    if target.status == "skipped" and by == "ai":
        raise OutlineError("cannot move to a skipped section")
    now = util.now()
    cur = o.by_id.get(session.live_section_id or "")
    result = MoveResult(prev_id=cur.id if cur else None, live_id=target.id, by=by)
    if cur is not None and cur.id == target.id:
        return result
    if cur is not None:
        _stop_timer(session, cur)
        result.changed.add(cur.id)
        forward = o.later(target, cur)
        cur_top = o.top(cur)
        target_top = o.top(target)
        if not forward:
            cur.status = "pending"
            if cur.parent_id and target_top.id != cur_top.id and cur_top.status in ("pending", "live"):
                cur_top.status = "pending"
        elif target.parent_id == cur.id:
            # Into one of its own sub-points: the point stays open.
            cur.status = "pending"
        else:
            cur.status = close_status
            if cur.parent_id:
                # Sub-points before the one we leave are covered too.
                result.changed.update(mark_subpoints_through(o, cur))
                if target_top.id != cur_top.id:
                    if cur_top.status in ("pending", "live"):
                        cur_top.status = close_status
                        cur_top.ended_at = now
                        result.changed.add(cur_top.id)
                    result.changed.update(_close_children(o, cur_top))
                    result.closed.append(cur_top.id)
            else:
                result.changed.update(_close_children(o, cur))
                result.closed.append(cur.id)
    # Re-entering a closed section (or a sub-point of a closed point).
    target_top = o.top(target)
    for s in {target.id: target, target_top.id: target_top}.values():
        if s.status in ("done", "deferred"):
            mid = _reopen_minutes(db, session, s.id)
            if mid:
                result.minutes_changed.append(mid)
            if s.id != target.id:
                s.status = "pending"
                result.changed.add(s.id)
    target.status = "live"
    # started_at = start of the current live run (paused: null).
    target.started_at = now if session.status == "running" else None
    target.ended_at = None
    result.changed.add(target.id)
    session.live_section_id = target.id
    session.topic_section_id = target.id
    st = _topic_state(session)
    st.update({"candidate": None, "later": None, "later_hist": [], "later_run": None, "hint": None})
    _set_topic_state(session, st)
    session.proposal_json = None
    if by == "ai":
        until = now + timedelta(seconds=AI_UNDO_SECONDS)
        session.undo_json = util.dumps({"from": result.prev_id, "to": target.id, "until": util.iso(until)})
        result.undo_until = util.iso(until)
    else:
        session.undo_json = None
        session.ai_moves_suppressed_until = now + timedelta(seconds=CHAIR_SUPPRESS_SECONDS)
    db.flush()
    return result


def _close_children(o: Outline, parent: MeetppSection) -> set[str]:
    """When a point closes, its pending sub-points are done (FDD §1a)."""
    changed = set()
    for c in o.children.get(parent.id, []):
        if c.status == "pending":
            c.status = "done"
            changed.add(c.id)
    return changed


def mark_subpoints_through(o: Outline, sub: MeetppSection, include_self: bool = False) -> set[str]:
    """The topic moved to sub-point `sub`: earlier pending siblings are done."""
    changed: set[str] = set()
    if not sub.parent_id:
        return changed
    for c in o.children.get(sub.parent_id, []):
        if c.id == sub.id:
            if include_self and c.status == "pending":
                c.status = "done"
                changed.add(c.id)
            break
        if c.status == "pending":
            c.status = "done"
            changed.add(c.id)
    return changed


def resolve_sub(o: Outline, section: MeetppSection | None, sub) -> MeetppSection | None:
    """Sub-point from a topic `sub` value: a label ("b"), a number ("3.2") or
    a prompt id ("S7")."""
    if section is None or sub in (None, ""):
        return None
    key = str(sub).strip().strip("()").lower()
    top = o.top(section)
    kids = o.children.get(top.id, [])
    if re.fullmatch(r"[a-z]", key):
        i = ord(key) - ord("a")
        return kids[i] if 0 <= i < len(kids) else None
    found = o.resolve(sub)
    if found is not None and found.parent_id == top.id:
        return found
    return None


def undo_info(session: MeetppSession) -> dict | None:
    u = util.loads(session.undo_json, {})
    if not u or not u.get("from"):
        return None
    until = util.parse_dt(u.get("until"))
    if until is None or util.now() > until:
        return None
    return {"from": u.get("from"), "to": u.get("to"), "until": u.get("until")}


def undo(db: Session, session: MeetppSession) -> MoveResult | None:
    info = undo_info(session)
    if info is None:
        return None
    o = load(db, session.id)
    back = o.by_id.get(info["from"])
    if back is None or session.live_section_id != info.get("to"):
        session.undo_json = None
        return None
    return move(db, session, back, by="undo")


def close_live(db: Session, session: MeetppSession) -> list[str]:
    """Close the live section at session end. Returns top-level ids to compose."""
    o = load(db, session.id)
    cur = o.by_id.get(session.live_section_id or "")
    if cur is None:
        return []
    _stop_timer(session, cur)
    cur.status = "done"
    top = o.top(cur)
    if top.id != cur.id and top.status in ("pending", "live"):
        top.status = "done"
        top.ended_at = util.now()
    _close_children(o, top)
    return [top.id]


def ai_moves_suppressed(session: MeetppSession) -> bool:
    until = util.aware(session.ai_moves_suppressed_until)
    return until is not None and util.now() < until


def timebox_overrun(db: Session, session: MeetppSession) -> MeetppSection | None:
    """Live section at ≥ 150 % of its timebox that was not nudged yet."""
    s = db.get(MeetppSection, session.live_section_id) if session.live_section_id else None
    if s is None or not s.timebox_minutes:
        return None
    if live_elapsed(session, s) < 1.5 * s.timebox_minutes * 60:
        return None
    st = _topic_state(session)
    nudged = st.get("nudged") or []
    if s.id in nudged:
        return None
    nudged.append(s.id)
    st["nudged"] = nudged
    _set_topic_state(session, st)
    return s
