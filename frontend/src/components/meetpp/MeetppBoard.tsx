import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronRight, Eye, EyeOff, FileText, Pencil, Play, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useMeetpp } from "../../lib/meetpp/store";
import { meetppApi } from "../../lib/meetpp/api";
import type { MeetppTab } from "../../lib/meetpp/types";

/**
 * Meet++ board — styled to the FDD mockup (Figure 2): a light white card with
 * blue/green/amber accents, phase pills, tab counters and per-tab tables.
 */

const PHASES: Array<{ key: string; label: string; labelKey: string }> = [
  { key: "opening", label: "Opening", labelKey: "meetpp.phases.opening" },
  { key: "previous_actions", label: "Prev. actions", labelKey: "meetpp.phases.previousActions" },
  { key: "agenda", label: "Agenda", labelKey: "meetpp.phases.agenda" },
  { key: "discussion", label: "Discussion", labelKey: "meetpp.phases.discussion" },
  { key: "aob", label: "AOB", labelKey: "meetpp.phases.aob" },
  { key: "new_actions", label: "New actions", labelKey: "meetpp.phases.newActions" },
  { key: "closing", label: "Closing", labelKey: "meetpp.phases.closing" },
];

const TABS: MeetppTab[] = ["agenda", "decisions", "actions", "attendance", "minutes", "attachments", "transcript"];
const TAB_LABEL: Record<MeetppTab, string> = {
  agenda: "Agenda",
  decisions: "Decisions",
  actions: "Actions",
  attendance: "Attendance",
  minutes: "Minutes",
  attachments: "Attachments",
  transcript: "Transcript",
};

const NEXT_PHASE: Record<string, string> = {
  opening: "previous_actions",
  previous_actions: "agenda",
  agenda: "discussion",
  discussion: "discussion:next_item",
  aob: "new_actions",
  new_actions: "closing",
  closing: "closing",
};

function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return now;
}

function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function statusPill(status: string): string {
  switch (status) {
    case "done":
      return "bg-emerald-100 text-emerald-700";
    case "carried":
      return "bg-indigo-100 text-indigo-700";
    case "open":
    case "in_progress":
      return "bg-blue-100 text-blue-700";
    case "proposed":
      return "bg-slate-100 text-slate-500";
    case "dropped":
      return "bg-rose-100 text-rose-600";
    case "confirmed":
      return "bg-emerald-100 text-emerald-700";
    case "active":
      return "bg-blue-600 text-white";
    case "deferred":
      return "bg-amber-100 text-amber-700";
    default:
      return "bg-slate-100 text-slate-600";
  }
}

interface Props {
  readOnly?: boolean;
  canEdit?: boolean;
  onSendOps?: (ops: Array<Record<string, unknown>>) => void;
  onJumpPhase?: (to: string, itemId?: string | null) => void;
  onAddSnapshot?: () => void;
  className?: string;
}

export default function MeetppBoard({ readOnly, canEdit, onSendOps, onJumpPhase, onAddSnapshot, className }: Props) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board);
  const session = useMeetpp((s) => s.session);
  const tab = useMeetpp((s) => s.tab);
  const setTab = useMeetpp((s) => s.setTab);
  const follow = useMeetpp((s) => s.follow);
  const toggleFollow = useMeetpp((s) => s.toggleFollow);
  const agent = useMeetpp((s) => s.agent);
  const unseen = useMeetpp((s) => s.unseen);
  const markSeen = useMeetpp((s) => s.markSeen);
  const focusId = useMeetpp((s) => s.focusId);
  const noteInteraction = useMeetpp((s) => s.noteInteraction);
  const highlights = useMeetpp((s) => s.highlights);
  const lastUpdatedAt = useMeetpp((s) => s.lastUpdatedAt);
  const captions = useMeetpp((s) => s.captions);
  const containerRef = useRef<HTMLDivElement>(null);
  const now = useNow();
  // Throttled polite live region (FDD §7.10: at most one summary / 10 s).
  const [liveMsg, setLiveMsg] = useState("");
  const lastAnnounceRef = useRef(0);

  useEffect(() => {
    markSeen(tab);
  }, [tab, markSeen]);

  useEffect(() => {
    if (!lastUpdatedAt) return;
    if (Date.now() - lastAnnounceRef.current < 10000) return;
    lastAnnounceRef.current = Date.now();
    setLiveMsg(
      t("meetpp.board.liveUpdated", {
        defaultValue: "Board updated: {{d}} decisions, {{a}} actions",
        d: board?.decisions.length ?? 0,
        a: board?.actions.length ?? 0,
      }),
    );
  }, [lastUpdatedAt, board, t]);

  useEffect(() => {
    if (!focusId || readOnly) return;
    const el = containerRef.current?.querySelector(`[data-meetpp-id="${focusId}"]`);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusId, tab, readOnly]);

  if (!board || !session) {
    return (
      <div className={`flex h-full w-full items-center justify-center bg-white text-sm text-slate-400 ${className ?? ""}`}>
        {t("meetpp.board.status.listening", { defaultValue: "Waiting for the board…" })}
      </div>
    );
  }

  const editing = canEdit && !readOnly;
  const activeItem = board.agenda.find((a) => a.id === session.current_item_id) ?? null;
  const phaseBase = (session.phase || "opening").split(":")[0];
  const idx = PHASES.findIndex((p) => p.key === phaseBase);
  const nextTarget = NEXT_PHASE[phaseBase] ?? "closing";

  const elapsed =
    activeItem && activeItem.started_at ? (now - new Date(activeItem.started_at).getTime()) / 1000 : 0;

  const counts: Partial<Record<MeetppTab, string>> = {
    decisions: String(board.decisions.length),
    actions: String(board.actions.length),
    attendance: `${board.attendance.filter((a) => a.presence === "present").length}/${board.attendance.length}`,
    attachments: String(board.attachments.length),
  };

  const dot =
    agent.status === "listening"
      ? "bg-emerald-500"
      : agent.status === "behind"
      ? "bg-amber-500"
      : agent.status === "offline"
      ? "bg-red-500"
      : agent.status === "paused"
      ? "bg-slate-400"
      : "bg-blue-500 animate-pulse";
  const statusText =
    agent.status === "behind" && agent.backlog_s != null
      ? t("meetpp.board.status.behind", { defaultValue: "Behind {{s}}s", s: Math.round(agent.backlog_s) })
      : t(`meetpp.board.status.${agent.status}`, { defaultValue: agent.status });

  const updatedAgo = lastUpdatedAt ? Math.max(0, Math.round((now - lastUpdatedAt) / 1000)) : 0;
  const lastCaption = captions[captions.length - 1] ?? null;

  return (
    <div
      ref={containerRef}
      onScrollCapture={noteInteraction}
      onClickCapture={noteInteraction}
      className={`flex h-full w-full flex-col overflow-hidden rounded-2xl bg-white text-slate-800 shadow-2xl ring-1 ring-slate-200 ${className ?? ""}`}
    >
      <div className="sr-only" aria-live="polite">{liveMsg}</div>
      {/* Header: wordmark + phase pills + timer/status/follow */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2">
        <span className="mr-1 text-lg font-extrabold tracking-tight text-slate-900">Meet++</span>
        <div className="flex flex-wrap items-center gap-1">
          {PHASES.map((p, i) => (
            <button
              key={p.key}
              type="button"
              disabled={!editing}
              onClick={() => onJumpPhase?.(p.key)}
              className={[
                "rounded-full px-2.5 py-0.5 text-[11px] font-medium transition",
                i === idx
                  ? "bg-blue-600 text-white"
                  : i < idx
                  ? "bg-slate-200 text-slate-500"
                  : "bg-slate-100 text-slate-500",
                editing ? "hover:bg-slate-300" : "cursor-default",
              ].join(" ")}
            >
              {i < idx ? "✓ " : ""}
              {t(p.labelKey, { defaultValue: p.label })}
              {p.key === "discussion" && board.agenda.length > 0 ? ` ${Math.max(1, activeItem?.position ?? 1)}/${board.agenda.length}` : ""}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2 text-[11px] text-slate-500">
          {activeItem && (
            <span className="font-medium text-slate-700">
              {t("meetpp.board.item", { defaultValue: "Item" })} {activeItem.position} • {fmtClock(elapsed)}
              {activeItem.timebox ? ` / ${fmtClock(activeItem.timebox * 60)}` : ""}
            </span>
          )}
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${dot}`} />
          <span>{statusText}</span>
          {editing && (
            <button
              type="button"
              onClick={() => onJumpPhase?.(nextTarget)}
              data-testid="btn-meetpp-next"
              className="inline-flex items-center gap-1 rounded-full bg-emerald-600 px-2.5 py-0.5 font-semibold text-white hover:bg-emerald-700"
            >
              {t("meetpp.phases.next", { defaultValue: "Next" })} <ChevronRight size={12} />
            </button>
          )}
          {!readOnly && (
            <button
              type="button"
              onClick={toggleFollow}
              className={[
                "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 font-medium",
                follow ? "bg-blue-600 text-white" : "bg-slate-200 text-slate-600",
              ].join(" ")}
            >
              {follow ? <Eye size={12} /> : <EyeOff size={12} />}
              {follow ? t("meetpp.board.follow", { defaultValue: "Follow" }) : t("meetpp.board.followPaused", { defaultValue: "Paused" })}
            </button>
          )}
        </div>
      </div>

      {/* Tabs with counters */}
      <div role="tablist" className="flex gap-4 overflow-x-auto border-b border-slate-200 px-4">
        {TABS.map((tb) => (
          <button
            key={tb}
            role="tab"
            aria-selected={tab === tb}
            onClick={() => setTab(tb)}
            className={[
              "relative flex items-center gap-1 whitespace-nowrap border-b-2 py-2 text-sm transition",
              tab === tb ? "border-blue-600 font-semibold text-blue-700" : "border-transparent text-slate-500 hover:text-slate-700",
            ].join(" ")}
          >
            {TAB_LABEL[tb]}
            {counts[tb] && (
              <span className={tab === tb ? "rounded-full bg-blue-600 px-1.5 text-[10px] text-white" : "text-[11px] text-slate-400"}>
                {counts[tb]}
              </span>
            )}
            {unseen[tb] && <span className="ml-0.5 inline-block h-1.5 w-1.5 rounded-full bg-amber-500 align-middle" />}
          </button>
        ))}
      </div>

      <div role="tabpanel" className="flex-1 overflow-y-auto px-4 py-3 text-sm">
        {tab === "agenda" && <AgendaTab canEdit={editing} onSendOps={onSendOps} />}
        {tab === "decisions" && <DecisionsTab canEdit={editing} onSendOps={onSendOps} highlights={highlights} />}
        {tab === "actions" && <ActionsTab canEdit={editing} onSendOps={onSendOps} highlights={highlights} />}
        {tab === "attendance" && <AttendanceTab canEdit={editing} onSendOps={onSendOps} sessionId={session.id} />}
        {tab === "minutes" && <MinutesTab canEdit={editing} onSendOps={onSendOps} sessionId={session.id} />}
        {tab === "attachments" && <AttachmentsTab canEdit={editing} onAddSnapshot={onAddSnapshot} sessionId={session.id} />}
        {tab === "transcript" && <TranscriptTab />}
      </div>

      {/* Footer: transcript bubble + update indicator */}
      <div className="flex items-center gap-3 border-t border-slate-200 bg-slate-50 px-4 py-1.5 text-[11px] text-slate-500">
        {lastCaption ? (
          <div className="mx-auto max-w-[70%] truncate rounded-lg bg-slate-800 px-3 py-1 text-slate-100">
            <span className="font-semibold">{lastCaption.name}: </span>
            {lastCaption.text}
          </div>
        ) : (
          <span className="mx-auto" />
        )}
        <span className="ml-auto whitespace-nowrap">
          {t("meetpp.board.updated", { defaultValue: "AI updated {{s}}s ago", s: updatedAgo })} · state v{session.version}
        </span>
      </div>
    </div>
  );
}

function Empty({ hint }: { hint: string }) {
  return <div className="py-8 text-center text-xs text-slate-400">{hint}</div>;
}

function Provenance({ origin, ref_, previous }: { origin: string; ref_?: string; previous?: boolean }) {
  if (previous) return <span className="italic text-slate-400">prev. meeting</span>;
  return <span className="italic text-slate-400">{origin === "ai" ? "AI" : origin}{ref_ ? ` · ${ref_}` : ""}</span>;
}

function AgendaTab({ canEdit, onSendOps }: { canEdit?: boolean; onSendOps?: Props["onSendOps"] }) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board)!;
  const addItem = () => {
    const title = window.prompt(t("meetpp.board.actions.addItemPrompt", { defaultValue: "Agenda item title" }));
    if (title) onSendOps?.([{ op: "agenda.add", title }]);
  };
  const editItem = (id: string, title: string, presenter: string | null, timebox: number | null) => {
    const newTitle = window.prompt(t("meetpp.board.actions.editItemPrompt", { defaultValue: "Agenda item title" }), title);
    if (newTitle === null) return;
    const presenter2 = window.prompt(t("meetpp.board.actions.presenterPrompt", { defaultValue: "Presenter (optional)" }), presenter ?? "");
    const tb = window.prompt(t("meetpp.board.actions.timeboxPrompt", { defaultValue: "Timebox minutes (optional)" }), timebox ? String(timebox) : "");
    onSendOps?.([
      {
        op: "agenda.update",
        item_id: id,
        title: newTitle || title,
        presenter: presenter2 ?? "",
        timebox_minutes: tb ? Number(tb) : null,
      },
    ]);
  };
  if (board.agenda.length === 0) {
    return (
      <div>
        <Empty hint={t("meetpp.board.empty.agenda", { defaultValue: "No agenda items yet." })} />
        {canEdit && <AddLink label={t("meetpp.board.actions.addItem", { defaultValue: "Add agenda item" })} onClick={addItem} />}
      </div>
    );
  }
  return (
    <div>
    <table className="w-full text-left text-sm">
      <thead className="text-[11px] uppercase tracking-wide text-slate-400">
        <tr>
          <th className="py-1 pr-3">#</th>
          <th className="pr-3">{t("meetpp.board.tabs.agenda", { defaultValue: "Agenda" })}</th>
          <th className="pr-3">{t("meetpp.report.member", { defaultValue: "Presenter" })}</th>
          <th className="pr-3">{t("pdf.status", { defaultValue: "Status" })}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {board.agenda.map((item) => (
          <tr key={item.id} data-meetpp-id={item.id} className={item.status === "active" ? "bg-blue-50" : ""}>
            <td className="py-1.5 pr-3 text-slate-400">{item.position}</td>
            <td className="pr-3 font-medium text-slate-800">
              {item.title}
              {item.timebox ? <span className="ml-2 text-xs text-slate-400">{item.timebox} min</span> : null}
            </td>
            <td className="pr-3 text-slate-600">{item.presenter ?? "—"}</td>
            <td className="pr-3"><span className={`rounded-full px-2 py-0.5 text-[11px] ${statusPill(item.status)}`}>{item.status}</span></td>
            <td className="text-right">
              {canEdit && (
                <span className="inline-flex gap-1">
                  {item.status !== "active" && (
                    <button className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] text-blue-700" onClick={() => onSendOps?.([{ op: "agenda.set_active", item_id: item.id }])}>
                      <Play size={11} className="inline" />
                    </button>
                  )}
                  <button className="rounded bg-emerald-100 px-1.5 py-0.5 text-[11px] text-emerald-700" onClick={() => onSendOps?.([{ op: "agenda.set_status", item_id: item.id, status: "done" }])}>
                    <Check size={11} className="inline" />
                  </button>
                  <button className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600" onClick={() => onSendOps?.([{ op: "agenda.set_status", item_id: item.id, status: "deferred" }])}>
                    <ChevronRight size={11} className="inline" />
                  </button>
                  <button className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600" onClick={() => editItem(item.id, item.title, item.presenter, item.timebox)}>
                    <Pencil size={11} className="inline" />
                  </button>
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
    {canEdit && <AddLink label={t("meetpp.board.actions.addItem", { defaultValue: "Add agenda item" })} onClick={addItem} />}
    </div>
  );
}

function AddLink({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="mt-2 text-sm font-semibold text-blue-600 hover:underline">
      + {label}
    </button>
  );
}

function RowActions({ id, canEdit, onSendOps, kind }: { id: string; canEdit?: boolean; onSendOps?: Props["onSendOps"]; kind: "decision" | "action" }) {
  if (!canEdit) return null;
  const op = kind === "decision" ? "decision.update" : "action.update";
  return (
    <span className="inline-flex gap-1">
      <button className="grid h-6 w-6 place-items-center rounded-full bg-emerald-500 text-white" title="confirm" onClick={() => onSendOps?.([{ op, id, status: kind === "decision" ? "confirmed" : "open" }])}>
        <Check size={12} />
      </button>
      <button className="grid h-6 w-6 place-items-center rounded-full bg-slate-200 text-slate-600" title="edit" onClick={() => {
        const v = window.prompt(kind === "decision" ? "Decision text" : "Owner name or alias");
        if (v) onSendOps?.([{ op, id, ...(kind === "decision" ? { text: v } : { owner_alias: v }) }]);
      }}>
        <Pencil size={12} />
      </button>
      <button className="grid h-6 w-6 place-items-center rounded-full bg-rose-500 text-white" title="reject" onClick={() => onSendOps?.([{ op, id, status: kind === "decision" ? "rejected" : "dropped" }])}>
        <X size={12} />
      </button>
    </span>
  );
}

function DecisionsTab({ canEdit, onSendOps, highlights }: { canEdit?: boolean; onSendOps?: Props["onSendOps"]; highlights: Record<string, number> }) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board)!;
  if (board.decisions.length === 0) return <Empty hint={t("meetpp.board.empty.decisions", { defaultValue: "Decisions will appear here as they are made." })} />;
  return (
    <div className="space-y-1.5">
      {board.decisions.map((d) => (
        <div key={d.id} data-meetpp-id={d.id} className={`flex items-start gap-3 rounded-lg px-3 py-2 ${highlights[d.id] && highlights[d.id] > Date.now() ? "bg-amber-50 ring-1 ring-amber-300" : "bg-slate-50"}`}>
          <span className="rounded bg-blue-600 px-1.5 py-0.5 text-[11px] font-semibold text-white">{d.ref}</span>
          <div className="min-w-0 flex-1">
            <div className="text-slate-800">{d.text}</div>
            {d.rationale && <div className="text-xs text-slate-500">{d.rationale}</div>}
          </div>
          <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusPill(d.status)}`}>{d.status}</span>
          <RowActions id={d.id} canEdit={canEdit} onSendOps={onSendOps} kind="decision" />
        </div>
      ))}
      {board.previous_decisions.length > 0 && (
        <details className="pt-2">
          <summary className="cursor-pointer text-xs text-slate-400">{t("meetpp.board.tabs.previousDecisions", { defaultValue: "Previous meeting's decisions" })}</summary>
          <ul className="mt-1 space-y-1 text-xs text-slate-500">
            {board.previous_decisions.map((d) => (
              <li key={d.id}><span className="mr-1 text-slate-400">{d.ref}</span>{d.text}</li>
            ))}
          </ul>
        </details>
      )}
      {canEdit && (
        <AddLink
          label={t("meetpp.board.actions.addDecision", { defaultValue: "Add decision" })}
          onClick={() => {
            const v = window.prompt(t("meetpp.board.actions.decisionPrompt", { defaultValue: "Decision text" }));
            if (v) onSendOps?.([{ op: "decision.add", text: v, evidence: [0] }]);
          }}
        />
      )}
    </div>
  );
}

const ACTION_STATUSES = ["open", "in_progress", "done", "dropped", "carried"];

function ActionsTab({ canEdit, onSendOps, highlights }: { canEdit?: boolean; onSendOps?: Props["onSendOps"]; highlights: Record<string, number> }) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board)!;
  const sessionId = board.session.id;
  const isOpen = (s: string) => s !== "done" && s !== "dropped";
  if (board.actions.length === 0) return <Empty hint={t("meetpp.board.empty.actions", { defaultValue: "Actions will appear here as they are agreed." })} />;
  const groups = [
    { title: t("meetpp.board.actions.previous", { defaultValue: "Previous (open)" }), items: board.actions.filter((a) => isOpen(a.status) && a.session_id && a.session_id !== sessionId) },
    { title: t("meetpp.board.actions.new", { defaultValue: "New this meeting" }), items: board.actions.filter((a) => !a.session_id || a.session_id === sessionId) },
  ];
  return (
    <>
    <table className="w-full text-left text-sm">
      <thead className="text-[11px] uppercase tracking-wide text-slate-400">
        <tr>
          <th className="py-1 pr-3">ID</th>
          <th className="pr-3">{t("meetpp.board.tabs.actions", { defaultValue: "Action" })}</th>
          <th className="pr-3">{t("pdf.owner")}</th>
          <th className="pr-3">{t("pdf.due")}</th>
          <th className="pr-3">{t("pdf.status")}</th>
          <th className="pr-3">Source</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {groups.map((g) => (
          <Fragment key={g.title}>
            <tr><td colSpan={7} className="pt-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">{g.title}</td></tr>
            {g.items.map((a) => {
              const hot = highlights[a.id] && highlights[a.id] > Date.now();
              const prev = a.session_id && a.session_id !== sessionId;
              return (
                <tr key={a.id} data-meetpp-id={a.id} className={hot ? "bg-amber-50 ring-1 ring-amber-300" : "border-t border-slate-100"}>
                  <td className="py-1.5 pr-3 text-slate-500">{a.ref}</td>
                  <td className="pr-3 font-medium text-slate-800">{a.title}</td>
                  <td className="pr-3 text-slate-600">{a.owner ?? "—"}</td>
                  <td className="pr-3 text-slate-600">{a.due ?? "—"}</td>
                  <td className="pr-3">
                    <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusPill(a.status)}`}>{a.status}</span>
                  </td>
                  <td className="pr-3"><Provenance origin={a.origin} ref_={a.ref} previous={!!prev} /></td>
                  <td className="text-right">
                    <span className="inline-flex items-center gap-1">
                      {canEdit && (
                        <select
                          className="rounded border border-slate-200 bg-white px-1 py-0.5 text-[11px] text-slate-600"
                          value=""
                          onChange={(e) => e.target.value && onSendOps?.([{ op: "action.update", id: a.id, status: e.target.value }])}
                        >
                          <option value="">status…</option>
                          {ACTION_STATUSES.map((st) => <option key={st} value={st}>{st}</option>)}
                        </select>
                      )}
                      <RowActions id={a.id} canEdit={canEdit} onSendOps={onSendOps} kind="action" />
                    </span>
                  </td>
                </tr>
              );
            })}
          </Fragment>
        ))}
      </tbody>
    </table>
    {canEdit && (
      <AddLink
        label={t("meetpp.board.actions.addAction", { defaultValue: "Add action" })}
        onClick={() => {
          const title = window.prompt(t("meetpp.board.actions.actionPrompt", { defaultValue: "Action" }));
          if (!title) return;
          const owner = window.prompt(t("meetpp.board.actions.ownerPrompt", { defaultValue: "Owner name or alias (optional)" })) || "";
          const due = window.prompt(t("meetpp.board.actions.duePrompt", { defaultValue: "Due date YYYY-MM-DD (optional)" })) || "";
          onSendOps?.([{ op: "action.add", title, owner_alias: owner || null, due: due || null, evidence: [0] }]);
        }}
      />
    )}
    </>
  );
}

function AttendanceTab({ canEdit, onSendOps, sessionId }: { canEdit?: boolean; onSendOps?: Props["onSendOps"]; sessionId: string }) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board)!;
  if (board.attendance.length === 0) return <Empty hint={t("meetpp.board.empty.attendance", { defaultValue: "Attendance will appear as people join." })} />;
  const edit = (id: string, name: string, email: string | null) => {
    const newName = window.prompt(t("meetpp.board.actions.namePrompt", { defaultValue: "Display name" }), name);
    if (newName === null) return;
    const newEmail = window.prompt(t("meetpp.board.actions.emailPrompt", { defaultValue: "E-mail (optional)" }), email ?? "");
    void meetppApi.patchAttendee(sessionId, id, { display_name: newName || name, email: newEmail ?? "" });
  };
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-[11px] uppercase tracking-wide text-slate-400">
        <tr><th className="py-1">Name</th><th>Presence</th><th>Talk</th><th>Next</th><th /></tr>
      </thead>
      <tbody>
        {board.attendance.map((a) => (
          <tr key={a.id} data-meetpp-id={a.id} className="border-t border-slate-100">
            <td className="py-1.5 text-slate-800">{a.name}</td>
            <td>
              <span className={`rounded-full px-2 py-0.5 text-[11px] ${a.opted_out ? "bg-slate-200 text-slate-500" : "bg-emerald-100 text-emerald-700"}`}>
                {a.opted_out ? t("meetpp.consent.notTranscribed", { defaultValue: "Not transcribed" }) : a.presence}
              </span>
            </td>
            <td className="text-slate-600">{Math.round(a.talk_seconds)}s</td>
            <td>
              {canEdit ? (
                <button className="rounded-full bg-blue-100 px-2 py-0.5 text-[11px] text-blue-700" onClick={() => onSendOps?.([{ op: "attendance.require_next", person: a.name, reason: "Manual" }])}>
                  {a.required_next ? "✓" : "+"}
                </button>
              ) : a.required_next ? "✓" : ""}
            </td>
            <td className="text-right">
              {canEdit && (
                <span className="inline-flex gap-1">
                  <button className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600" title="apologies" onClick={() => void meetppApi.patchAttendee(sessionId, a.id, { presence: "apologies", required_next: true })}>
                    {t("meetpp.board.actions.apologies", { defaultValue: "Apologies" })}
                  </button>
                  <button className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600" onClick={() => edit(a.id, a.name, a.email)}>
                    <Pencil size={11} className="inline" />
                  </button>
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MinutesTab({ canEdit, onSendOps, sessionId }: { canEdit?: boolean; onSendOps?: Props["onSendOps"]; sessionId: string }) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board)!;
  if (board.agenda.length === 0) return <Empty hint={t("meetpp.board.empty.minutes", { defaultValue: "Minutes will appear per agenda item." })} />;
  return (
    <div className="space-y-4">
      {board.agenda.map((item) => {
        const minute = board.minutes.find((m) => m.item_id === item.id);
        return (
          <div key={item.id} data-meetpp-id={minute?.id ?? item.id}>
            <div className="flex items-center justify-between">
              <div className="text-sm font-semibold text-slate-800">{item.position}. {item.title}{item.presenter ? ` — ${item.presenter}` : ""}</div>
              {canEdit && (
                <button
                  className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
                  onClick={() => void meetppApi.regenerateMinutes(sessionId, item.id)}
                >
                  <RefreshCw size={11} /> {t("meetpp.review.regenerate", { defaultValue: "Regenerate" })}
                </button>
              )}
            </div>
            {canEdit ? (
              <textarea
                className="mt-1 w-full rounded border border-slate-200 bg-white p-2 text-sm text-slate-700"
                rows={3}
                defaultValue={minute?.body_md ?? ""}
                onFocus={() => useMeetpp.getState().setEditing(true)}
                onBlur={(e) => {
                  useMeetpp.getState().setEditing(false);
                  const body = e.target.value;
                  if (body !== (minute?.body_md ?? "")) onSendOps?.([{ op: "minutes.upsert", item_id: item.id, body_md: body }]);
                }}
              />
            ) : (
              <div className="mt-1 whitespace-pre-wrap text-sm text-slate-600">{minute?.body_md || "—"}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AttachmentsTab({ canEdit, onAddSnapshot, sessionId }: { canEdit?: boolean; onAddSnapshot?: () => void; sessionId: string }) {
  const { t } = useTranslation();
  const board = useMeetpp((s) => s.board)!;
  return (
    <div>
      {canEdit && (
        <button className="mb-2 inline-flex items-center gap-1 rounded-full bg-emerald-600 px-3 py-1 text-xs font-medium text-white" onClick={onAddSnapshot}>
          <Plus size={12} /> {t("meetpp.snapshot.toolbarButton", { defaultValue: "Snapshot to Meet++" })}
        </button>
      )}
      <div className="grid grid-cols-2 gap-2">
        {board.attachments.map((a) =>
          a.kind === "whiteboard" ? (
            <div key={a.id} data-meetpp-id={a.id} className="relative rounded-lg border border-slate-200 bg-slate-50 p-1">
              <a href={a.url} target="_blank" rel="noreferrer">
                <img src={a.url} alt={a.caption ?? ""} className="w-full rounded" />
                <div className="mt-1 text-[11px] text-slate-500">{a.caption}</div>
              </a>
              {canEdit && (
                <button
                  className="absolute right-1.5 top-1.5 grid h-6 w-6 place-items-center rounded-full bg-white/90 text-rose-600 shadow"
                  title={t("meetpp.board.actions.delete", { defaultValue: "Delete" })}
                  onClick={() => void meetppApi.deleteAttachment(sessionId, a.id)}
                >
                  <Trash2 size={12} />
                </button>
              )}
            </div>
          ) : (
            <a key={a.id} data-meetpp-id={a.id} href={a.url} target="_blank" rel="noreferrer" className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs text-slate-700 hover:bg-slate-100">
              <FileText size={14} className="shrink-0 text-blue-600" />
              <span className="truncate">{a.caption ?? a.filename}</span>
            </a>
          ),
        )}
      </div>
      {board.attachments.length === 0 && <Empty hint={t("meetpp.board.empty.attachments", { defaultValue: "No attachments yet." })} />}
    </div>
  );
}

function TranscriptTab() {
  const { t } = useTranslation();
  const transcript = useMeetpp((s) => s.transcript);
  const [q, setQ] = useState("");
  const filtered = useMemo(
    () => (q.trim() ? transcript.filter((s) => (s.text + " " + (s.name ?? "")).toLowerCase().includes(q.trim().toLowerCase())) : transcript),
    [transcript, q],
  );
  return (
    <div className="space-y-2">
      <div className="relative">
        <Search size={14} className="absolute left-2 top-2 text-slate-400" />
        <input
          className="w-full rounded-lg border border-slate-200 bg-white py-1.5 pl-7 pr-2 text-sm text-slate-700"
          placeholder={t("meetpp.board.searchTranscript", { defaultValue: "Search the transcript…" })}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {filtered.length === 0 ? (
        <Empty hint={t("meetpp.board.empty.transcript", { defaultValue: "The transcript appears here while the meeting runs." })} />
      ) : (
        <div className="space-y-1.5">
          {filtered.map((s) => (
            <div key={`t${s.seq}`} className="text-sm">
              <span className="mr-1 font-semibold text-blue-700">{s.name ?? s.identity}</span>
              <span className="text-slate-700">{s.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
