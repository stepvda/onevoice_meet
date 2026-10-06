import { useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, MoreHorizontal, Plus } from "lucide-react";
import { effectiveViewed, useMeetpp, viewSection } from "../../lib/meetpp/store";
import { meetppActions, refetchState } from "../../lib/meetpp/session";
import { meetppApi, type OutlinePointInput } from "../../lib/meetpp/api";
import { fmtDuration, outlineOrder, sectionElapsed, sectionLabel } from "../../lib/meetpp/state";
import type { SectionDto } from "../../lib/meetpp/types";
import { InlineInput, cx, useNow, useOnClickOutside } from "./ui";

/**
 * Meeting outline (FDD §5.4): one ordered tree of sections with a single
 * LIVE marker (red outline) and this user's VIEWING row (blue fill). Clicking
 * a row only changes the view (and pauses follow for 10 s); chair / editors
 * get a row menu. ARIA tree pattern.
 */

interface Props {
  variant?: "full" | "rail";
  canChair: boolean;
  canEdit: boolean;
  onPicked?: () => void;
  onExpandRail?: () => void;
}

export default function MeetppOutline({ variant = "full", canChair, canEdit, onPicked, onExpandRail }: Props) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  const viewed = useMeetpp((s) => effectiveViewed(s));
  const now = useNow(1000);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [addingSub, setAddingSub] = useState<string | null>(null);
  const [addingPoint, setAddingPoint] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const treeRef = useRef<HTMLUListElement>(null);

  const sections = useMemo(() => (snap ? outlineOrder(snap.sections) : []), [snap]);
  const hasChildren = useMemo(() => {
    const set = new Set<string>();
    for (const s of sections) if (s.parent_id) set.add(s.parent_id);
    return set;
  }, [sections]);
  const previousStats = useMemo(() => {
    const prev = (snap?.actions ?? []).filter((a) => a.previous);
    return { total: prev.length, reported: prev.filter((a) => a.report).length };
  }, [snap]);

  if (!snap) return null;
  const live = snap.session.live_section_id;
  // A skipped "Previous actions" is not shown: the agenda's own actions point
  // (or nothing to review) took its place.
  const rows = sections.filter(
    (s) => !(s.parent_id && collapsed[s.parent_id]) && !(s.kind === "previous_actions" && s.status === "skipped"),
  );

  const run = async (p: Promise<unknown>) => {
    setError(null);
    try {
      await p;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const pick = (s: SectionDto) => {
    viewSection(s.id, true);
    onPicked?.();
  };

  const onKey = (e: ReactKeyboardEvent<HTMLElement>, s: SectionDto, index: number) => {
    const items = treeRef.current?.querySelectorAll<HTMLElement>("[role=treeitem]");
    const focusAt = (i: number) => items?.[Math.max(0, Math.min((items?.length ?? 1) - 1, i))]?.focus();
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusAt(index + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusAt(index - 1);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      pick(s);
    } else if (e.key === "ArrowRight" && hasChildren.has(s.id)) {
      setCollapsed((c) => ({ ...c, [s.id]: false }));
    } else if (e.key === "ArrowLeft" && hasChildren.has(s.id)) {
      setCollapsed((c) => ({ ...c, [s.id]: true }));
    }
  };

  if (variant === "rail") {
    return (
      <nav className="flex h-full flex-col items-center gap-1 overflow-y-auto py-2" aria-label={t("meetpp.outline.title", { defaultValue: "Meeting outline" })}>
        <button
          type="button"
          onClick={onExpandRail}
          className="mb-1 rounded p-1 text-slate-500 hover:bg-slate-200"
          aria-label={t("meetpp.outline.expand", { defaultValue: "Show the meeting outline" })}
        >
          <ChevronRight size={16} />
        </button>
        {sections
          .filter((s) => !s.parent_id)
          .map((s) => {
            const isLive = s.id === live;
            const isViewed = s.id === viewed && !isLive;
            return (
              <button
                key={s.id}
                type="button"
                title={sectionLabel(s)}
                aria-label={sectionLabel(s)}
                onClick={() => pick(s)}
                className={cx(
                  "grid h-8 w-8 flex-shrink-0 place-items-center rounded-md text-xs font-semibold",
                  isLive ? "border-2 border-red-500 text-red-700" : "border border-transparent",
                  isViewed ? "bg-blue-100 text-blue-800 ring-2 ring-blue-500" : "",
                  s.status === "done" && !isLive && !isViewed ? "text-emerald-700" : "text-slate-600",
                  s.status === "skipped" && "opacity-40",
                )}
              >
                {s.number ?? s.title.charAt(0).toUpperCase()}
              </button>
            );
          })}
      </nav>
    );
  }

  return (
    <nav className="flex h-full min-h-0 flex-col" aria-label={t("meetpp.outline.title", { defaultValue: "Meeting outline" })}>
      <div className="px-3 pb-1 pt-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">
        {t("meetpp.outline.title", { defaultValue: "Meeting outline" })}
      </div>
      <ul ref={treeRef} role="tree" aria-label={t("meetpp.outline.title", { defaultValue: "Meeting outline" })} className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {rows.map((s, index) => {
          const isLive = s.id === live;
          const isViewed = s.id === viewed;
          const sub = !!s.parent_id;
          const parent = hasChildren.has(s.id);
          const elapsed = isLive ? sectionElapsed(s, now) : 0;
          const tb = s.timebox_minutes ? s.timebox_minutes * 60 : 0;
          const over = isLive && tb > 0 && elapsed > tb;
          const nudge = isLive && tb > 0 && elapsed >= tb * 1.5 && canChair;
          const counts =
            (s.previous_actions_home || s.kind === "previous_actions") && previousStats.total > 0
              ? `${previousStats.reported}/${previousStats.total}`
              : s.counts && (s.counts.decisions || s.counts.actions)
                ? `${s.counts.decisions}·${s.counts.actions}`
                : "";
          return (
            <li key={s.id} className="relative">
              <div
                role="treeitem"
                aria-level={sub ? 2 : 1}
                aria-selected={isViewed}
                aria-expanded={parent ? !collapsed[s.id] : undefined}
                aria-current={isLive ? "step" : undefined}
                tabIndex={isViewed ? 0 : -1}
                onClick={() => pick(s)}
                onKeyDown={(e) => onKey(e, s, index)}
                onContextMenu={(e: ReactMouseEvent) => {
                  if (!canEdit) return;
                  e.preventDefault();
                  setMenuFor(s.id);
                }}
                className={cx(
                  "group relative my-0.5 flex cursor-pointer items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-blue-400",
                  sub && "ml-5 text-[12px]",
                  isLive ? "border-2 border-red-500" : "border-2 border-transparent",
                  isViewed && !isLive ? "bg-blue-600 text-white" : isViewed ? "bg-blue-50" : "hover:bg-slate-100",
                  s.status === "skipped" && "opacity-50",
                )}
              >
                {parent ? (
                  <button
                    type="button"
                    tabIndex={-1}
                    onClick={(e) => {
                      e.stopPropagation();
                      setCollapsed((c) => ({ ...c, [s.id]: !c[s.id] }));
                    }}
                    className={cx("-ml-1 rounded p-0.5", isViewed && !isLive ? "text-white" : "text-slate-400")}
                    aria-label={collapsed[s.id] ? t("meetpp.outline.expandRow", { defaultValue: "Expand" }) : t("meetpp.outline.collapseRow", { defaultValue: "Collapse" })}
                  >
                    {collapsed[s.id] ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  </button>
                ) : null}
                <StatusIcon status={s.status} live={isLive} inverted={isViewed && !isLive} />
                {renaming === s.id ? (
                  <div className="min-w-0 flex-1" onClick={(e) => e.stopPropagation()}>
                    <InlineInput
                      value={s.title}
                      ariaLabel={t("meetpp.outline.rename", { defaultValue: "Rename" })}
                      onCancel={() => setRenaming(null)}
                      onSave={(v) => {
                        setRenaming(null);
                        if (v.trim() && v.trim() !== s.title) void run(meetppActions.ops([{ op: "section.update", id: s.id, title: v.trim() }]));
                      }}
                    />
                  </div>
                ) : (
                  <span className="min-w-0 flex-1">
                    <span className={cx("block truncate", isLive && "font-semibold", s.status === "done" && !isViewed && "text-slate-500")} title={sectionLabel(s)}>
                      {s.number ? <span className="mr-1">{s.number} ·</span> : null}
                      {s.title}
                    </span>
                    {isLive && (
                      <span className={cx("block text-[10px] tabular-nums", over ? "font-semibold text-amber-600" : "text-slate-500")}>
                        {fmtDuration(elapsed)}
                        {tb ? ` / ${fmtDuration(tb)}` : ""}
                      </span>
                    )}
                  </span>
                )}
                {counts && !isLive && !isViewed && (
                  <span className="text-[10px] tabular-nums text-slate-400 group-hover:invisible" title={t("meetpp.outline.countsHint", { defaultValue: "decisions · actions" })}>
                    {counts}
                  </span>
                )}
                {isLive && (
                  <span className={cx("flex-shrink-0 text-[10px] font-bold text-red-600 group-hover:invisible", nudge && "motion-safe:animate-pulse")}>
                    {t("meetpp.outline.live", { defaultValue: "LIVE" })}
                  </span>
                )}
                {isViewed && !isLive && (
                  <span className="flex-shrink-0 text-[10px] font-bold text-white group-hover:invisible">{t("meetpp.outline.viewing", { defaultValue: "VIEWING" })}</span>
                )}
                {canEdit && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setMenuFor((m) => (m === s.id ? null : s.id));
                    }}
                    className={cx(
                      "absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 opacity-0 shadow-sm focus:opacity-100 group-hover:opacity-100",
                      isViewed && !isLive ? "bg-blue-600 text-white hover:bg-blue-500" : "bg-white text-slate-500 hover:bg-slate-200",
                      menuFor === s.id && "opacity-100",
                    )}
                    aria-label={t("meetpp.outline.menu", { defaultValue: "Section actions" })}
                    aria-haspopup="menu"
                  >
                    <MoreHorizontal size={14} />
                  </button>
                )}
              </div>
              {menuFor === s.id && (
                <RowMenu
                  section={s}
                  isLive={isLive}
                  canChair={canChair}
                  onClose={() => setMenuFor(null)}
                  onRename={() => setRenaming(s.id)}
                  onAddSub={() => setAddingSub(s.id)}
                  run={run}
                />
              )}
              {addingSub === s.id && (
                <div className="ml-8 mr-1 py-1">
                  <InlineInput
                    value=""
                    placeholder={t("meetpp.outline.subPlaceholder", { defaultValue: "Sub-point title" })}
                    ariaLabel={t("meetpp.outline.addSub", { defaultValue: "Add sub-point" })}
                    onCancel={() => setAddingSub(null)}
                    onSave={(v) => {
                      setAddingSub(null);
                      if (v.trim()) void run(meetppActions.ops([{ op: "section.add", parent_id: s.id, title: v.trim(), kind: "agenda" }]));
                    }}
                  />
                </div>
              )}
            </li>
          );
        })}
        {canEdit && (
          <li className="mt-1">
            {addingPoint ? (
              <InlineInput
                value=""
                placeholder={t("meetpp.outline.pointPlaceholder", { defaultValue: "Agenda point title" })}
                ariaLabel={t("meetpp.outline.addPoint", { defaultValue: "Add agenda point" })}
                onCancel={() => setAddingPoint(false)}
                onSave={(v) => {
                  setAddingPoint(false);
                  if (v.trim()) void run(meetppActions.ops([{ op: "section.add", title: v.trim(), kind: "agenda" }]));
                }}
              />
            ) : (
              <button type="button" onClick={() => setAddingPoint(true)} className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-blue-700 hover:bg-blue-50">
                <Plus size={12} /> {t("meetpp.outline.addPoint", { defaultValue: "Add agenda point" })}
              </button>
            )}
          </li>
        )}
      </ul>
      {error && <div className="mx-2 mb-2 rounded bg-rose-50 px-2 py-1 text-[11px] text-rose-700">{error}</div>}
    </nav>
  );
}

function StatusIcon({ status, live, inverted }: { status: SectionDto["status"]; live: boolean; inverted: boolean }) {
  if (live) return <span className="w-3 flex-shrink-0 text-center text-red-600">●</span>;
  const map: Record<string, [string, string]> = {
    done: ["✓", "text-emerald-600"],
    deferred: ["⤳", "text-amber-600"],
    skipped: ["–", "text-slate-400"],
    pending: ["○", "text-slate-400"],
    live: ["○", "text-slate-400"],
  };
  const [ch, cls] = map[status] ?? map.pending;
  return <span className={cx("w-3 flex-shrink-0 text-center text-xs", inverted ? "text-white" : cls)}>{ch}</span>;
}

function RowMenu({
  section,
  isLive,
  canChair,
  onClose,
  onRename,
  onAddSub,
  run,
}: {
  section: SectionDto;
  isLive: boolean;
  canChair: boolean;
  onClose: () => void;
  onRename: () => void;
  onAddSub: () => void;
  run: (p: Promise<unknown>) => Promise<void>;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  useOnClickOutside(ref, onClose);
  const snap = useMeetpp((s) => s.snap);
  const empty =
    section.kind === "agenda" &&
    !section.counts?.decisions &&
    !section.counts?.actions &&
    !(snap?.sections ?? []).some((x) => x.parent_id === section.id) &&
    !(snap?.minutes ?? []).some((m) => m.section_id === section.id && (m.notes.length > 0 || m.narrative_md));
  const item = (label: string, fn: () => void, danger = false) => (
    <button
      type="button"
      role="menuitem"
      onClick={() => {
        onClose();
        fn();
      }}
      className={cx("block w-full rounded px-2.5 py-1.5 text-left text-[13px]", danger ? "text-rose-600 hover:bg-rose-50" : "text-slate-700 hover:bg-slate-100")}
    >
      {label}
    </button>
  );
  return (
    <div ref={ref} role="menu" className="absolute right-1 top-full z-30 w-56 rounded-lg border border-slate-200 bg-white p-1 shadow-xl">
      {canChair && !isLive && item(t("meetpp.outline.moveHere", { defaultValue: "Move meeting here" }), () => void run(meetppActions.move(section.id)))}
      {section.status !== "done" && item(t("meetpp.outline.markDone", { defaultValue: "Mark done" }), () => void run(meetppActions.ops([{ op: "section.status", id: section.id, status: "done" }])))}
      {section.status !== "deferred" &&
        item(t("meetpp.outline.defer", { defaultValue: "Defer to next meeting" }), () => void run(meetppActions.ops([{ op: "section.status", id: section.id, status: "deferred" }])))}
      {item(t("meetpp.outline.rename", { defaultValue: "Rename" }), onRename)}
      {!section.parent_id && section.kind === "agenda" && item(t("meetpp.outline.addSub", { defaultValue: "Add sub-point" }), onAddSub)}
      {empty && !isLive && item(t("meetpp.outline.delete", { defaultValue: "Delete" }), () => void run(deleteAgendaSection(section.id)), true)}
    </div>
  );
}

/** Delete an empty agenda point / sub-point: PUT /outline without it. */
async function deleteAgendaSection(id: string): Promise<void> {
  const snap = useMeetpp.getState().snap;
  if (!snap) return;
  const ordered = outlineOrder(snap.sections);
  const tops = ordered.filter((s) => s.kind === "agenda" && !s.parent_id && s.id !== id);
  const agenda: OutlinePointInput[] = tops.map((p) => ({
    id: p.id,
    title: p.title,
    body: p.body,
    presenter: p.presenter,
    timebox_minutes: p.timebox_minutes,
    subpoints: ordered.filter((c) => c.parent_id === p.id && c.id !== id).map((c) => ({ id: c.id, title: c.title, body: c.body })),
  }));
  await meetppApi.putOutline(snap.sid, agenda);
  await refetchState();
}
