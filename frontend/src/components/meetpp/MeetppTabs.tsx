import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Camera, ChevronDown, ChevronRight, CornerDownLeft, Pencil, Plus } from "lucide-react";
import {
  backToLive,
  effectiveViewed,
  interact,
  markTabSeen,
  selectTab,
  toggleGroup,
  useMeetpp,
} from "../../lib/meetpp/store";
import { meetppActions } from "../../lib/meetpp/session";
import { MIN_DISPLAY_MS } from "../../lib/meetpp/focusQueue";
import { actionGroupSection, decisionGroupSection, outlineOrder, sectionLabel } from "../../lib/meetpp/state";
import { TABS, type ActionDto, type DecisionDto, type SectionDto, type Tab } from "../../lib/meetpp/types";
import {
  ActionCard,
  AddItem,
  AttachmentCard,
  AttendanceTable,
  DecisionCard,
  DocumentRow,
  EditForm,
  ErrorLine,
  MinutesBlock,
  SmallBtn,
  useRun,
} from "./MeetppItems";
import { InlineInput, Pill, cx, useReducedMotion } from "./ui";

/**
 * Content tabs (FDD §5.5): Agenda · Decisions · Actions · Minutes ·
 * Attendance · Papers — the sections of the final report — with counters,
 * unseen dots and the activation highlight + 5 s progress bar. Content is
 * grouped per outline section. WAI-ARIA tab pattern; activations change the
 * visible tab without moving keyboard focus.
 */

interface Props {
  canChair: boolean;
  canEdit: boolean;
  /** Participants in the room may add whiteboard snapshots. */
  canSnapshot: boolean;
  readOnly?: boolean;
  compact?: boolean;
}

export function useTabLabel() {
  const { t } = useTranslation();
  return (tab: Tab) =>
    ({
      agenda: t("meetpp.tabs.agenda", { defaultValue: "Agenda" }),
      decisions: t("meetpp.tabs.decisions", { defaultValue: "Decisions" }),
      actions: t("meetpp.tabs.actions", { defaultValue: "Actions" }),
      minutes: t("meetpp.tabs.minutes", { defaultValue: "Minutes" }),
      attendance: t("meetpp.tabs.attendance", { defaultValue: "Attendance" }),
      papers: t("meetpp.tabs.papers", { defaultValue: "Papers" }),
    })[tab];
}

export default function MeetppTabs({ canChair, canEdit, canSnapshot, readOnly, compact }: Props) {
  const { t } = useTranslation();
  const label = useTabLabel();
  const snap = useMeetpp((s) => s.snap);
  const tab = useMeetpp((s) => s.tab);
  const unseen = useMeetpp((s) => s.unseen);
  const highlight = useMeetpp((s) => s.highlight);
  const viewed = useMeetpp((s) => effectiveViewed(s));
  const scrollReq = useMeetpp((s) => s.scrollReq);
  const reduced = useReducedMotion();
  const paneRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  // A tab being looked at is seen.
  useEffect(() => {
    markTabSeen(tab);
  }, [tab, snap?.version]);

  // Programmatic scroll (activation / outline click / back to live). Never
  // counted as an interaction: only wheel / key / click handlers call interact().
  useEffect(() => {
    if (!scrollReq) return;
    const pane = paneRef.current;
    if (!pane) return;
    const raf = requestAnimationFrame(() => {
      const target =
        (scrollReq.itemId && pane.querySelector<HTMLElement>(`[data-item="${CSS.escape(scrollReq.itemId)}"]`)) ||
        (scrollReq.sectionId && pane.querySelector<HTMLElement>(`[data-group="${CSS.escape(scrollReq.sectionId)}"]`)) ||
        null;
      if (!target) return;
      const top = target.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop - 8;
      const want = scrollReq.itemId ? top - pane.clientHeight / 3 : top;
      pane.scrollTo({ top: Math.max(0, want), behavior: reduced ? "auto" : "smooth" });
    });
    return () => cancelAnimationFrame(raf);
  }, [scrollReq, tab, reduced]);

  const counts = useMemo(() => {
    if (!snap) return {} as Partial<Record<Tab, string>>;
    const present = snap.attendees.filter((a) => a.status === "present" || a.status === "represented").length;
    return {
      decisions: String(snap.decisions.filter((d) => !d.previous).length),
      actions: String(snap.actions.length),
      attendance: `${present}/${snap.attendees.length}`,
      papers: String(snap.attachments.length + snap.documents.length),
    } as Partial<Record<Tab, string>>;
  }, [snap]);

  if (!snap) return null;
  const live = snap.session.live_section_id;
  const liveSection = snap.sections.find((s) => s.id === live) ?? null;
  const viewedSection = snap.sections.find((s) => s.id === viewed) ?? null;

  const onTabKey = (e: ReactKeyboardEvent, i: number) => {
    let j = -1;
    if (e.key === "ArrowRight") j = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = TABS.length - 1;
    if (j < 0) return;
    e.preventDefault();
    selectTab(TABS[j], true);
    tabRefs.current[TABS[j]]?.focus();
  };

  const manual = readOnly ? undefined : interact;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div role="tablist" aria-label={t("meetpp.tabs.label", { defaultValue: "Meeting record" })} className="flex flex-shrink-0 gap-1 overflow-x-auto border-b border-slate-200 px-2 pt-1">
        {TABS.map((tb, i) => {
          const active = tb === tab;
          const hot = highlight?.tab === tb;
          return (
            <button
              key={tb}
              ref={(el) => (tabRefs.current[tb] = el)}
              role="tab"
              id={`meetpp-tab-${tb}`}
              aria-selected={active}
              aria-controls="meetpp-tabpanel"
              tabIndex={active ? 0 : -1}
              onClick={() => selectTab(tb, true)}
              onKeyDown={(e) => onTabKey(e, i)}
              className={cx(
                "relative flex-shrink-0 whitespace-nowrap rounded-t-lg border-b-2 px-3 py-2 text-[13px] font-medium",
                hot ? "border-amber-500 bg-amber-100 text-amber-900 ring-2 ring-amber-400" : active ? "border-blue-600 text-blue-700" : "border-transparent text-slate-600 hover:text-slate-900",
              )}
            >
              {label(tb)}
              {counts[tb] ? <span className="ml-1 tabular-nums">{counts[tb]}</span> : null}
              {unseen[tb] && !active && (
                <span className="absolute right-0.5 top-1 h-2 w-2 rounded-full bg-amber-500" aria-label={t("meetpp.tabs.unseen", { defaultValue: "new changes" })} />
              )}
            </button>
          );
        })}
      </div>
      <ProgressBar startedAt={highlight?.startedAt ?? null} reduced={reduced} />
      {viewed && live && viewed !== live && viewedSection && (
        <div className="mx-2 mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[13px] text-blue-900">
          <span className="min-w-0 flex-1">
            {t("meetpp.viewing.text", {
              defaultValue: "You are viewing {{viewed}}. The meeting is at {{live}}.",
              viewed: sectionLabel(viewedSection),
              live: sectionLabel(liveSection),
            })}
          </span>
          <button type="button" onClick={() => backToLive()} className="inline-flex items-center gap-1 rounded-full bg-blue-700 px-3 py-1 text-xs font-semibold text-white hover:bg-blue-800">
            {t("meetpp.viewing.back", { defaultValue: "Back to live" })} <CornerDownLeft size={12} />
          </button>
        </div>
      )}
      <div
        ref={paneRef}
        id="meetpp-tabpanel"
        role="tabpanel"
        aria-labelledby={`meetpp-tab-${tab}`}
        tabIndex={0}
        onWheel={manual}
        onTouchMove={manual}
        onKeyDown={(e) => {
          if (["PageDown", "PageUp", "ArrowDown", "ArrowUp", "Home", "End", " "].includes(e.key) && e.target === e.currentTarget) manual?.();
        }}
        onClickCapture={manual}
        onInputCapture={manual}
        className={cx("min-h-0 flex-1 overflow-y-auto px-3 py-2 outline-none", compact && "text-[13px]")}
      >
        {tab === "agenda" && <AgendaTab canEdit={canEdit && !readOnly} />}
        {tab === "decisions" && <DecisionsTab canEdit={canEdit && !readOnly} canVote={canChair && !readOnly} />}
        {tab === "actions" && <ActionsTab canEdit={canEdit && !readOnly} />}
        {tab === "minutes" && <MinutesTab canChair={canChair && !readOnly} canEdit={canEdit && !readOnly} />}
        {tab === "attendance" && <AttendanceTab canEdit={canEdit && !readOnly} />}
        {tab === "papers" && <PapersTab canEdit={canEdit && !readOnly} canSnapshot={canSnapshot && !readOnly} />}
      </div>
    </div>
  );
}

function ProgressBar({ startedAt, reduced }: { startedAt: number | null; reduced: boolean }) {
  const [pct, setPct] = useState(0);
  useEffect(() => {
    if (startedAt === null) {
      setPct(0);
      return;
    }
    let raf = 0;
    const tick = () => {
      const p = Math.min(1, (Date.now() - startedAt) / MIN_DISPLAY_MS);
      setPct(p);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    if (reduced) setPct(1);
    else tick();
    return () => cancelAnimationFrame(raf);
  }, [startedAt, reduced]);
  return (
    <div className="h-1 flex-shrink-0 bg-transparent" aria-hidden>
      {startedAt !== null && (
        <div className="h-full bg-amber-200">
          <div className="h-full bg-amber-500" style={{ width: `${pct * 100}%` }} />
        </div>
      )}
    </div>
  );
}

// ── Grouping ──────────────────────────────────────────────────────────────

interface GroupDef<T> {
  section: SectionDto | null;
  key: string;
  title: string;
  items: T[];
  depth: number;
}

/** Groups in outline order (sub-points as nested groups), then "Other". */
function useGroups<T>(itemsBySection: Map<string | null, T[]>, includeEmpty = true): GroupDef<T>[] {
  const snap = useMeetpp((s) => s.snap);
  const { t } = useTranslation();
  return useMemo(() => {
    if (!snap) return [];
    const out: GroupDef<T>[] = [];
    for (const s of outlineOrder(snap.sections)) {
      const items = itemsBySection.get(s.id) ?? [];
      if (!includeEmpty && items.length === 0) continue;
      if (s.status === "skipped" && items.length === 0) continue;
      out.push({ section: s, key: s.id, title: sectionLabel(s), items, depth: s.parent_id ? 1 : 0 });
    }
    const other = itemsBySection.get(null) ?? [];
    if (other.length) out.push({ section: null, key: "__other", title: t("meetpp.groups.other", { defaultValue: "Not linked to a section" }), items: other, depth: 0 });
    return out;
  }, [snap, itemsBySection, includeEmpty, t]);
}

function GroupList<T>({
  tab,
  groups,
  render,
  emptyExtra,
  defaultOpen,
  newCount,
}: {
  tab: Tab;
  groups: GroupDef<T>[];
  render: (item: T) => ReactNode;
  emptyExtra?: (g: GroupDef<T>) => ReactNode;
  defaultOpen?: (g: GroupDef<T>) => boolean;
  newCount?: (g: GroupDef<T>) => number;
}) {
  const live = useMeetpp((s) => s.snap?.session.live_section_id ?? null);
  const viewed = useMeetpp((s) => effectiveViewed(s));
  const expanded = useMeetpp((s) => s.expanded);
  const highlight = useMeetpp((s) => s.highlight);
  const out: ReactNode[] = [];
  let emptyRun: GroupDef<T>[] = [];
  const flushEmpty = () => {
    if (!emptyRun.length) return;
    const run = emptyRun;
    emptyRun = [];
    out.push(
      <div key={`empty-${run[0].key}`} className="flex flex-wrap gap-x-3 gap-y-0.5 py-1 text-[12px] text-slate-400">
        {run.map((g) => (
          <button key={g.key} type="button" data-group={g.key} onClick={() => toggleGroup(tab, g.key, true)} className={cx("inline-flex items-center gap-0.5 hover:text-slate-600", g.depth > 0 && "ml-4")}>
            <ChevronRight size={11} /> {g.title} (0)
          </button>
        ))}
      </div>,
    );
  };
  for (const g of groups) {
    const key = `${tab}:${g.key}`;
    const isLive = g.key === live;
    const isViewed = g.key === viewed;
    const open = expanded[key] ?? (defaultOpen ? defaultOpen(g) : isLive || isViewed);
    if (g.items.length === 0 && !expanded[key] && !isViewed && !(isLive && emptyExtra)) {
      emptyRun.push(g);
      continue;
    }
    flushEmpty();
    const hot = highlight?.tab === tab && highlight.sectionId === g.key;
    const n = newCount ? newCount(g) : 0;
    out.push(
      <div key={g.key} data-group={g.key} className={cx("mb-1.5", g.depth > 0 && "ml-4")}>
        <button
          type="button"
          onClick={() => toggleGroup(tab, g.key, !open)}
          aria-expanded={open}
          className={cx(
            "flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-[14px] font-semibold",
            hot ? "bg-amber-100 text-amber-900" : isViewed ? "bg-blue-50 text-blue-900" : "text-slate-700 hover:bg-slate-50",
          )}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span className="min-w-0 flex-1 truncate">
            {g.title} <span className="font-normal text-slate-400">({g.items.length})</span>
          </span>
          {isLive && <span className="text-[10px] font-bold text-red-600">LIVE</span>}
          {n > 0 && !open && <span className="text-[11px] font-semibold text-amber-700">● {n}</span>}
        </button>
        {open && (
          <div className="mt-1 space-y-2 pl-1">
            {g.items.map(render)}
            {emptyExtra?.(g)}
          </div>
        )}
      </div>,
    );
  }
  flushEmpty();
  return <>{out}</>;
}

function useBadgeCounter() {
  const badges = useMeetpp((s) => s.badges);
  return <T extends { id: string }>(g: GroupDef<T>) => g.items.filter((i) => badges[i.id]).length;
}

// ── Agenda ────────────────────────────────────────────────────────────────

/** One block per top-level section: description, presenter, timebox and the
 * sub-points as a checklist (○ pending / ● live / ✓ done). */
function AgendaTab({ canEdit }: { canEdit: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap)!;
  const viewed = useMeetpp((s) => effectiveViewed(s));
  const highlight = useMeetpp((s) => s.highlight);
  const [editing, setEditing] = useState<string | null>(null);
  const [addingSub, setAddingSub] = useState<string | null>(null);
  const [run, error] = useRun();
  const ordered = useMemo(() => outlineOrder(snap.sections), [snap.sections]);
  const tops = useMemo(() => ordered.filter((s) => !s.parent_id || !snap.sections.some((p) => p.id === s.parent_id)), [ordered, snap.sections]);
  const children = useMemo(() => {
    const m = new Map<string, SectionDto[]>();
    for (const s of ordered) if (s.parent_id) m.set(s.parent_id, [...(m.get(s.parent_id) ?? []), s]);
    return m;
  }, [ordered]);
  const noteCount = useMemo(() => {
    const m = new Map<string, number>();
    for (const x of snap.minutes) if (x.section_id) m.set(x.section_id, (m.get(x.section_id) ?? 0) + x.notes.length);
    return m;
  }, [snap.minutes]);
  const live = snap.session.live_section_id;
  const liveParent = snap.sections.find((s) => s.id === live)?.parent_id ?? null;
  const statusText = (s: SectionDto) =>
    s.id === live
      ? t("meetpp.agenda.live", { defaultValue: "live" })
      : ({
          done: t("meetpp.agenda.done", { defaultValue: "done" }),
          deferred: t("meetpp.agenda.deferred", { defaultValue: "deferred to next meeting" }),
          pending: t("meetpp.agenda.pending", { defaultValue: "upcoming" }),
          skipped: t("meetpp.agenda.skipped", { defaultValue: "skipped" }),
          live: t("meetpp.agenda.pending", { defaultValue: "upcoming" }),
        })[s.status];
  const visible = tops.filter((s) => s.status !== "skipped");
  return (
    <div className="space-y-1.5">
      {snap.session.template === "goal" && snap.session.goal && (
        <div className="rounded-lg bg-slate-50 px-3 py-2 text-[13px] text-slate-700">
          <b>{t("meetpp.agenda.goal", { defaultValue: "Goal:" })}</b> {snap.session.goal}
        </div>
      )}
      {visible.map((s) => {
        const subs = children.get(s.id) ?? [];
        const notes = noteCount.get(s.id) ?? 0;
        const isLive = s.id === live || s.id === liveParent;
        const isViewed = s.id === viewed || subs.some((c) => c.id === viewed);
        const hot = highlight?.tab === "agenda" && (highlight.sectionId === s.id || subs.some((c) => c.id === highlight.sectionId));
        const showAdd = canEdit && s.kind === "agenda" && (isViewed || isLive || addingSub === s.id);
        const meta = [
          s.presenter ? t("meetpp.agenda.presenter", { defaultValue: "Presenter: {{name}}", name: s.presenter }) : "",
          s.timebox_minutes ? t("meetpp.agenda.timebox", { defaultValue: "{{n}} min", n: s.timebox_minutes }) : "",
          notes > 0 ? t("meetpp.agenda.notes", { defaultValue: "{{n}} running notes", n: notes }) : "",
        ].filter(Boolean);
        return (
          <div
            key={s.id}
            data-group={s.id}
            className={cx(
              "group rounded-xl border px-3 py-2",
              hot ? "border-amber-400 bg-amber-50" : isViewed ? "border-blue-300 bg-blue-50/60" : "border-slate-200 bg-white",
              s.status === "done" && !isViewed && !hot && "opacity-80",
            )}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className={cx("min-w-0 flex-1 text-[14px] font-semibold", isLive ? "text-slate-900" : "text-slate-700")}>{sectionLabel(s)}</span>
              <Pill tone={s.id === live ? "red" : s.status === "done" ? "green" : s.status === "deferred" ? "amber" : "slate"}>{statusText(s)}</Pill>
              {canEdit && editing !== s.id && (
                <span className="opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                  <SmallBtn onClick={() => setEditing(s.id)}>
                    <Pencil size={11} /> {t("meetpp.item.edit", { defaultValue: "Edit" })}
                  </SmallBtn>
                </span>
              )}
            </div>
            {meta.length > 0 && <div className="mt-0.5 text-[11px] text-slate-500">{meta.join(" · ")}</div>}
            {editing === s.id ? (
              <EditForm
                fields={[
                  { key: "title", label: t("meetpp.agenda.title", { defaultValue: "Title" }), value: s.title },
                  { key: "body", label: t("meetpp.agenda.body", { defaultValue: "Description" }), value: s.body ?? "", multiline: true },
                  { key: "presenter", label: t("meetpp.agenda.presenterLabel", { defaultValue: "Presenter" }), value: s.presenter ?? "" },
                  { key: "timebox_minutes", label: t("meetpp.agenda.timeboxLabel", { defaultValue: "Timebox (minutes)" }), value: s.timebox_minutes ? String(s.timebox_minutes) : "", type: "number" },
                ]}
                onCancel={() => setEditing(null)}
                onSave={async (c) => {
                  const patch: Record<string, unknown> = { ...c };
                  if ("timebox_minutes" in patch) patch.timebox_minutes = c.timebox_minutes ? Number(c.timebox_minutes) : null;
                  if (Object.keys(patch).length === 0) return setEditing(null);
                  if (await run(meetppActions.ops([{ op: "section.update", id: s.id, ...patch }]))) setEditing(null);
                }}
              />
            ) : (
              s.body && <div className="mt-1 whitespace-pre-wrap text-[13px] text-slate-700">{s.body}</div>
            )}
            {subs.length > 0 && (
              <ul className="mt-1.5 space-y-1">
                {subs.map((c) => (
                  <li key={c.id} data-group={c.id} className={cx("flex items-start gap-2 rounded px-1 text-[13px]", c.id === viewed && "bg-blue-100/70")}>
                    <span className={cx("mt-0.5 w-3 text-center", c.id === live ? "text-red-600" : c.status === "done" ? "text-emerald-600" : "text-slate-400")}>
                      {c.id === live ? "●" : c.status === "done" ? "✓" : c.status === "deferred" ? "⤳" : "○"}
                    </span>
                    <span className={cx("min-w-0 flex-1", c.status === "done" && "text-slate-500")}>
                      <span className="mr-1 text-slate-400">{c.number}</span>
                      {c.title}
                      {c.body && <span className="block text-[12px] text-slate-500">{c.body}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {showAdd && (
              <div className="mt-1">
                {addingSub === s.id ? (
                  <InlineInput
                    value=""
                    placeholder={t("meetpp.outline.subPlaceholder", { defaultValue: "Sub-point title" })}
                    ariaLabel={t("meetpp.outline.addSub", { defaultValue: "Add sub-point" })}
                    onCancel={() => setAddingSub(null)}
                    onSave={async (v) => {
                      if (!v.trim()) return setAddingSub(null);
                      if (await run(meetppActions.ops([{ op: "section.add", parent_id: s.id, title: v.trim(), kind: "agenda" }]))) setAddingSub(null);
                    }}
                  />
                ) : (
                  <button type="button" onClick={() => setAddingSub(s.id)} className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-blue-700 hover:bg-blue-50">
                    <Plus size={11} /> {t("meetpp.outline.addSub", { defaultValue: "Add sub-point" })}
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
      {visible.length === 0 && <Empty text={t("meetpp.agenda.empty", { defaultValue: "No agenda points yet." })} />}
      {error && <ErrorLine text={error} />}
    </div>
  );
}

/** Manual "Add decision / action" only where it makes sense: the viewed or
 * live group of an agenda-like section. */
function useAddAllowed() {
  const live = useMeetpp((s) => s.snap?.session.live_section_id ?? null);
  const viewed = useMeetpp((s) => effectiveViewed(s));
  return (g: GroupDef<unknown>) =>
    !!g.section && (g.key === live || g.key === viewed) && !["opening", "previous_actions", "closing"].includes(g.section.kind);
}

// ── Decisions ─────────────────────────────────────────────────────────────

function refNum(ref: string): number {
  const m = /(\d+)/.exec(ref);
  return m ? Number(m[1]) : 0;
}

function DecisionsTab({ canEdit, canVote }: { canEdit: boolean; canVote: boolean }) {
  const { t } = useTranslation();
  const addAllowed = useAddAllowed();
  const snap = useMeetpp((s) => s.snap)!;
  const counter = useBadgeCounter();
  const bySection = useMemo(() => {
    const m = new Map<string | null, DecisionDto[]>();
    for (const d of snap.decisions.filter((x) => !x.previous).sort((a, b) => refNum(a.ref) - refNum(b.ref))) {
      const k = decisionGroupSection(d, snap.sections);
      m.set(k, [...(m.get(k) ?? []), d]);
    }
    return m;
  }, [snap.decisions, snap.sections]);
  const previous = useMemo(() => snap.decisions.filter((d) => d.previous), [snap.decisions]);
  const groups = useGroups(bySection);
  const [showPrev, setShowPrev] = useState(false);
  return (
    <>
      <GroupList
        tab="decisions"
        groups={groups}
        newCount={counter}
        render={(d) => <DecisionCard key={d.id} d={d} canEdit={canEdit} canVote={canVote} />}
        emptyExtra={(g) => (canEdit && addAllowed(g) && g.section ? <AddItem kind="decision" sectionId={g.section.id} /> : null)}
      />
      {snap.decisions.length === 0 && <Empty text={t("meetpp.decisions.empty", { defaultValue: "Decisions appear here under their agenda point as they are taken." })} />}
      {previous.length > 0 && (
        <div className="mt-3 border-t border-slate-200 pt-2">
          <button type="button" className="flex items-center gap-1 text-[13px] font-semibold text-slate-500" onClick={() => setShowPrev((v) => !v)}>
            {showPrev ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {t("meetpp.decisions.previous", { defaultValue: "Earlier meetings of the series ({{n}})", n: previous.length })}
          </button>
          {showPrev && (
            <div className="mt-1 space-y-2">
              {previous.map((d) => (
                <DecisionCard key={d.id} d={d} canEdit={false} />
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}

// ── Actions ───────────────────────────────────────────────────────────────

function ActionsTab({ canEdit }: { canEdit: boolean }) {
  const { t } = useTranslation();
  const addAllowed = useAddAllowed();
  const snap = useMeetpp((s) => s.snap)!;
  const counter = useBadgeCounter();
  const bySection = useMemo(() => {
    const m = new Map<string | null, ActionDto[]>();
    const sorted = snap.actions.slice().sort((a, b) => Number(b.previous) - Number(a.previous) || refNum(a.ref) - refNum(b.ref));
    for (const a of sorted) {
      const k = actionGroupSection(a, snap.sections);
      m.set(k, [...(m.get(k) ?? []), a]);
    }
    return m;
  }, [snap.actions, snap.sections]);
  const groups = useGroups(bySection);
  return (
    <>
      <GroupList
        tab="actions"
        groups={groups}
        newCount={counter}
        render={(a) => <ActionCard key={a.id} a={a} canEdit={canEdit} />}
        emptyExtra={(g) => (canEdit && addAllowed(g) && g.section ? <AddItem kind="action" sectionId={g.section.id} /> : null)}
      />
      {snap.actions.length === 0 && <Empty text={t("meetpp.actions.empty", { defaultValue: "Actions appear here under their agenda point as they are agreed." })} />}
    </>
  );
}

// ── Minutes ───────────────────────────────────────────────────────────────

function MinutesTab({ canChair, canEdit }: { canChair: boolean; canEdit: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap)!;
  const live = snap.session.live_section_id;
  const bySection = useMemo(() => {
    const m = new Map<string | null, string[]>();
    for (const s of snap.sections) {
      const mm = snap.minutes.find((x) => x.section_id === s.id && (x.kind === "section" || !x.kind));
      const has = !!mm && (mm.notes.length > 0 || !!mm.narrative_md || mm.status === "composing" || mm.status === "failed");
      m.set(s.id, has || s.id === live ? [s.id] : []);
    }
    return m;
  }, [snap.sections, snap.minutes, live]);
  const groups = useGroups(bySection);
  const parts = snap.minutes.filter((m) => m.kind && m.kind !== "section" && (m.narrative_md || m.notes.length));
  const partLabel = (k: string) =>
    ({
      opening: t("meetpp.minutes.opening", { defaultValue: "Opening" }),
      adjournment: t("meetpp.minutes.adjournment", { defaultValue: "Adjournment" }),
      voting_record: t("meetpp.minutes.votingRecord", { defaultValue: "Record of voting" }),
      provenance: t("meetpp.minutes.provenance", { defaultValue: "Provenance" }),
    })[k] ?? k;
  return (
    <>
      {parts.length > 0 && (
        <div className="mb-2 space-y-2">
          {parts.map((m) => (
            <div key={m.id} className="rounded-xl border border-slate-200 bg-white px-3 py-2">
              <div className="mb-1 text-[13px] font-semibold text-slate-700">{partLabel(m.kind)}</div>
              <MinutesBlock m={m} section={null} isLive={false} canChair={false} canEdit={canEdit} />
            </div>
          ))}
        </div>
      )}
      <GroupList
        tab="minutes"
        groups={groups}
        render={(id) => {
          const s = snap.sections.find((x) => x.id === id) ?? null;
          const m = snap.minutes.find((x) => x.section_id === id && (x.kind === "section" || !x.kind)) ?? null;
          return (
            <div key={id} className="rounded-xl border border-slate-200 bg-white px-3 py-2">
              <MinutesBlock m={m} section={s} isLive={id === live} canChair={canChair} canEdit={canEdit} />
            </div>
          );
        }}
      />
    </>
  );
}

// ── Attendance ────────────────────────────────────────────────────────────

function AttendanceTab({ canEdit }: { canEdit: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap)!;
  const q = snap.quorum;
  const formal = snap.session.meeting_type !== "informal";
  const sorted = useMemo(() => {
    const rank: Record<string, number> = { present: 0, represented: 1, not_registered: 2, excused: 3, absent: 4 };
    return snap.attendees.slice().sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || a.name.localeCompare(b.name));
  }, [snap.attendees]);
  return (
    <div>
      {formal && q && (
        <div className={cx("mb-2 rounded-lg px-3 py-1.5 text-[13px]", q.met ? "bg-emerald-50 text-emerald-900" : "bg-amber-50 text-amber-900")}>
          {t("meetpp.attendance.quorum", {
            defaultValue: "Quorum: {{req}} required · {{present}} of {{total}} voting members present or represented · {{state}}",
            req: q.required ?? "—",
            present: q.voting_present,
            total: q.voting_total,
            state: q.met ? t("meetpp.attendance.quorumMet", { defaultValue: "met" }) : t("meetpp.attendance.quorumNotMet", { defaultValue: "not met" }),
          })}
        </div>
      )}
      <AttendanceTable attendees={sorted} canEdit={canEdit} />
    </div>
  );
}

// ── Papers ────────────────────────────────────────────────────────────────

function PapersTab({ canEdit, canSnapshot }: { canEdit: boolean; canSnapshot: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap)!;
  const roomName = useMeetpp((s) => s.ctx.roomName);
  const [run, error] = useRun();
  const [busy, setBusy] = useState(false);
  const bySection = useMemo(() => {
    const m = new Map<string | null, typeof snap.attachments>();
    for (const a of snap.attachments) {
      const k = a.section_id && snap.sections.some((s) => s.id === a.section_id) ? a.section_id : null;
      m.set(k, [...(m.get(k) ?? []), a]);
    }
    return m;
  }, [snap.attachments, snap.sections]);
  const groups = useGroups(bySection, false);
  return (
    <div onClick={(e) => e.stopPropagation()}>
      {canSnapshot && roomName && (
        <div className="mb-2">
          <SmallBtn
            tone="blue"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await run(meetppActions.snapshotWhiteboard(roomName));
              setBusy(false);
            }}
          >
            <Camera size={12} /> {t("meetpp.papers.snapshot", { defaultValue: "Add whiteboard snapshot" })}
          </SmallBtn>
        </div>
      )}
      {snap.documents.length > 0 && (
        <div className="mb-3 space-y-1.5">
          <div className="text-[12px] font-semibold uppercase tracking-wide text-slate-500">{t("meetpp.papers.documents", { defaultValue: "Meeting papers" })}</div>
          {snap.documents.map((d) => (
            <DocumentRow key={d.id} doc={d} />
          ))}
        </div>
      )}
      {groups.length > 0 && (
        <GroupList
          tab="papers"
          groups={groups}
          defaultOpen={() => true}
          render={(a) => <AttachmentCard key={a.id} att={a} canEdit={canEdit} />}
        />
      )}
      {snap.documents.length === 0 && snap.attachments.length === 0 && <Empty text={t("meetpp.papers.empty", { defaultValue: "Uploaded papers and whiteboard snapshots appear here." })} />}
      {error && <ErrorLine text={error} />}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="py-6 text-center text-sm text-slate-400">{text}</div>;
}

