import { useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronLeft, ChevronRight, Loader2, MessageSquareText, Pause, Play, RotateCcw, X } from "lucide-react";
import {
  backToLive,
  dismissAnnouncement,
  resumeFollow,
  toggleFollow,
  useMeetpp,
} from "../../lib/meetpp/store";
import { meetppActions } from "../../lib/meetpp/session";
import { fmtDuration, nextSection, prevSection, sectionElapsed, sectionLabel } from "../../lib/meetpp/state";
import MeetppOutline from "./MeetppOutline";
import MeetppTabs from "./MeetppTabs";
import MeetppTranscript from "./MeetppTranscript";
import { cx, useElementWidth, useNow, useOnClickOutside } from "./ui";

/**
 * The Meet++ board (FDD §5): one component for everyone. Header (LIVE chip,
 * chair Back / Next, AI status, Follow chip), meeting outline, content tabs
 * and the live transcript column. Chair / co-hosts and designated editors get
 * inline edit controls; everyone else navigates read-only.
 *
 * Layout follows the board's own width (§5.11): ≥1200 three panes;
 * 900–1199 numbered rail + 300 px transcript; 640–899 transcript drawer and
 * outline dropdown; <640 single column with a segmented control.
 */

export type BoardVariant = "stage" | "drawer" | "egress" | "public" | "preview";

interface Props {
  variant: BoardVariant;
  className?: string;
  /** Egress narrows the transcript column to 260 px. */
  transcriptWidth?: number;
  onClose?: () => void;
}

type Layout = "wide" | "medium" | "narrow" | "phone";

export default function MeetppBoard({ variant, className, transcriptWidth, onClose }: Props) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLDivElement>(null);
  const width = useElementWidth(rootRef);
  const snap = useMeetpp((s) => s.snap);
  const ctx = useMeetpp((s) => s.ctx);
  const liveMsg = useMeetpp((s) => s.liveMsg);
  const announcement = useMeetpp((s) => s.announcement);
  const [railOpen, setRailOpen] = useState(false);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [segment, setSegment] = useState<"outline" | "tabs" | "transcript">("tabs");

  const readOnly = variant === "egress" || variant === "public" || variant === "preview";
  const canChair = !readOnly && ctx.mode === "participant" && ctx.isChair;
  const canEdit = canChair || (!readOnly && ctx.mode === "participant" && !!ctx.localIdentity && !!snap?.session.editors?.includes(ctx.localIdentity));
  const canSnapshot = !readOnly && ctx.mode === "participant";
  // Egress: a recording frame or a grid cell of it — no interactive drawers.
  const layout: Layout =
    variant === "egress" ? (width >= 900 ? "wide" : "narrow") : width >= 1200 ? "wide" : width >= 900 ? "medium" : width >= 640 ? "narrow" : "phone";
  const tw = transcriptWidth ?? (layout === "wide" ? 340 : 300);

  if (!snap) {
    // Same root element as the loaded board, so the width observer keeps
    // measuring the right node.
    return (
      <div ref={rootRef} className={cx("relative flex h-full w-full flex-col overflow-hidden rounded-2xl bg-slate-50 text-slate-800 shadow-2xl ring-1 ring-blue-300", className)} data-testid="meetpp-board">
        <div className="flex flex-1 items-center justify-center text-sm text-slate-400">
          <Loader2 size={16} className="mr-2 animate-spin" />
          {t("meetpp.board.loading", { defaultValue: "Loading the Meet++ board…" })}
        </div>
      </div>
    );
  }

  const outline = <MeetppOutline canChair={canChair} canEdit={canEdit} />;
  const tabs = <MeetppTabs canChair={canChair} canEdit={canEdit} canSnapshot={canSnapshot} readOnly={readOnly} compact={variant === "egress"} />;
  const transcript = <MeetppTranscript variant="live" noSearch={variant === "egress"} />;

  return (
    <div
      ref={rootRef}
      className={cx("relative flex h-full w-full flex-col overflow-hidden rounded-2xl bg-slate-50 text-slate-800 shadow-2xl ring-1 ring-blue-300", className)}
      data-testid="meetpp-board"
    >
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {liveMsg?.text}
      </div>
      <Header
        layout={layout}
        variant={variant}
        canChair={canChair}
        onClose={onClose}
        onToggleOutline={() => setOutlineOpen((v) => !v)}
        outlineOpen={outlineOpen}
        onToggleTranscript={() => setTranscriptOpen((v) => !v)}
        transcriptOpen={transcriptOpen}
      />
      {canChair && <ChairBar />}
      {layout === "narrow" && outlineOpen && (
        <Popover onClose={() => setOutlineOpen(false)} className="left-2 top-14 h-[70%] w-72">
          <MeetppOutline canChair={canChair} canEdit={canEdit} onPicked={() => setOutlineOpen(false)} />
        </Popover>
      )}
      {layout === "phone" && (
        <div role="tablist" className="flex flex-shrink-0 gap-1 border-b border-slate-200 bg-white p-1">
          {(["outline", "tabs", "transcript"] as const).map((seg) => (
            <button
              key={seg}
              role="tab"
              aria-selected={segment === seg}
              onClick={() => setSegment(seg)}
              className={cx("flex-1 rounded-md py-1 text-xs font-medium", segment === seg ? "bg-blue-600 text-white" : "text-slate-600")}
            >
              {seg === "outline"
                ? t("meetpp.board.segOutline", { defaultValue: "Outline" })
                : seg === "tabs"
                  ? t("meetpp.board.segTabs", { defaultValue: "Record" })
                  : t("meetpp.board.segTranscript", { defaultValue: "Transcript" })}
            </button>
          ))}
        </div>
      )}
      <div className="relative flex min-h-0 flex-1">
        {layout === "wide" && <aside className="w-[220px] flex-shrink-0 border-r border-slate-200 bg-white">{outline}</aside>}
        {layout === "medium" && (
          <aside className="w-12 flex-shrink-0 border-r border-slate-200 bg-white">
            <MeetppOutline variant="rail" canChair={canChair} canEdit={canEdit} onExpandRail={() => setRailOpen(true)} />
          </aside>
        )}
        {layout === "medium" && railOpen && (
          <Popover onClose={() => setRailOpen(false)} className="bottom-0 left-12 top-0 max-h-none w-72">
            <MeetppOutline canChair={canChair} canEdit={canEdit} onPicked={() => setRailOpen(false)} />
          </Popover>
        )}
        {layout === "phone" ? (
          <div className="min-w-0 flex-1 bg-white">
            {segment === "outline" && <MeetppOutline canChair={canChair} canEdit={canEdit} onPicked={() => setSegment("tabs")} />}
            {segment === "tabs" && tabs}
            {segment === "transcript" && transcript}
          </div>
        ) : (
          <main className="min-w-0 flex-1 bg-white">{tabs}</main>
        )}
        {(layout === "wide" || layout === "medium") && (
          <aside className="flex-shrink-0 border-l border-slate-200" style={{ width: tw }}>
            {transcript}
          </aside>
        )}
        {layout === "narrow" && transcriptOpen && (
          <aside className="absolute bottom-0 right-0 top-0 z-20 border-l border-slate-200 bg-white shadow-2xl" style={{ width: 300 }}>
            <button type="button" className="absolute right-1 top-1.5 z-10 rounded p-1 text-slate-500 hover:bg-slate-100" onClick={() => setTranscriptOpen(false)} aria-label={t("meetpp.common.close", { defaultValue: "Close" })}>
              <X size={14} />
            </button>
            {transcript}
          </aside>
        )}
      </div>
      {layout === "phone" && canChair && <PhoneChairBar />}
      {announcement && <Banner coverRight={layout === "wide" || layout === "medium" ? tw : 0} canChair={canChair} onDismiss={dismissAnnouncement} />}
    </div>
  );
}

// ── Header ────────────────────────────────────────────────────────────────

function Header({
  layout,
  variant,
  canChair,
  onClose,
  onToggleOutline,
  outlineOpen,
  onToggleTranscript,
  transcriptOpen,
}: {
  layout: Layout;
  variant: BoardVariant;
  canChair: boolean;
  onClose?: () => void;
  onToggleOutline: () => void;
  outlineOpen: boolean;
  onToggleTranscript: () => void;
  transcriptOpen: boolean;
}) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap)!;
  const live = snap.sections.find((s) => s.id === snap.session.live_section_id) ?? null;
  const phone = layout === "phone";
  return (
    <header className="flex flex-shrink-0 flex-wrap items-center gap-2 border-b border-blue-200 bg-blue-50 px-3 py-2">
      {!phone && <span className="text-lg font-extrabold tracking-tight text-blue-900">Meet++</span>}
      {layout === "narrow" && (
        <button type="button" onClick={onToggleOutline} aria-expanded={outlineOpen} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700">
          {t("meetpp.outline.title", { defaultValue: "Meeting outline" })} <ChevronDown size={12} />
        </button>
      )}
      <LiveChip section={live} compact={phone} />
      {canChair && !phone && <NavButtons />}
      {!phone && variant !== "preview" && <AgentStatus compact={layout === "narrow"} />}
      <span className="flex-1" />
      {layout === "narrow" && <TranscriptToggle open={transcriptOpen} onToggle={onToggleTranscript} />}
      {variant !== "egress" && variant !== "preview" && <FollowChip />}
      {variant === "egress" && (
        <span className="inline-flex items-center gap-1 rounded-full border border-emerald-400 bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-800">
          ⟳ {t("meetpp.follow.following", { defaultValue: "Following live" })}
        </span>
      )}
      {onClose && (
        <button type="button" onClick={onClose} className="rounded p-1 text-slate-500 hover:bg-blue-100" aria-label={t("meetpp.common.close", { defaultValue: "Close" })}>
          <X size={16} />
        </button>
      )}
    </header>
  );
}

export function LiveChip({ section, compact }: { section: { id: string; number: string | null; title: string; timebox_minutes: number | null } | null; compact?: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  const now = useNow(1000);
  const full = snap?.sections.find((s) => s.id === section?.id) ?? null;
  const elapsed = full ? sectionElapsed(full, now) : 0;
  const tb = section?.timebox_minutes ? section.timebox_minutes * 60 : 0;
  const over = tb > 0 && elapsed > tb;
  const status = snap?.session.status;
  return (
    <button
      type="button"
      onClick={() => backToLive()}
      title={t("meetpp.header.backToLive", { defaultValue: "Back to live" })}
      className="inline-flex min-w-0 max-w-full items-center gap-2 rounded-full border-2 border-blue-700 bg-white px-3 py-1 text-[13px] font-semibold text-blue-900 hover:bg-blue-50"
    >
      <span className={cx("h-2.5 w-2.5 flex-shrink-0 rounded-full", status === "paused" || status === "setup" ? "bg-slate-400" : "bg-red-600")} />
      <span className="min-w-0 truncate">
        {status === "setup"
          ? t("meetpp.header.preview", { defaultValue: "PREVIEW · not started" })
          : `${status === "paused" ? t("meetpp.header.paused", { defaultValue: "PAUSED" }) : t("meetpp.header.live", { defaultValue: "LIVE" })} ${
              section ? sectionLabel(section) : t("meetpp.header.notStarted", { defaultValue: "not started" })
            }`}
      </span>
      {!compact && section && (
        <span className={cx("flex-shrink-0 font-normal tabular-nums", over ? "font-semibold text-amber-600" : "text-slate-600")}>
          {fmtDuration(elapsed)}
          {tb ? ` / ${fmtDuration(tb)}` : ""}
        </span>
      )}
    </button>
  );
}

function NavButtons({ compact }: { compact?: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap)!;
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const next = nextSection(snap.sections, snap.session.live_section_id);
  const prev = prevSection(snap.sections, snap.session.live_section_id);
  const go = async (what: "next" | "back") => {
    setBusy(what);
    setErr(null);
    try {
      await (what === "next" ? meetppActions.next() : meetppActions.back());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        disabled={!prev || !!busy}
        onClick={() => void go("back")}
        className="inline-flex items-center gap-0.5 rounded-full border-2 border-blue-700 bg-white px-3 py-1 text-[13px] font-medium text-blue-900 hover:bg-blue-50 disabled:opacity-40"
        title={prev ? sectionLabel(prev) : undefined}
      >
        <ChevronLeft size={14} /> {t("meetpp.header.back", { defaultValue: "Back" })}
      </button>
      <button
        type="button"
        disabled={!next || !!busy}
        onClick={() => void go("next")}
        data-testid="btn-meetpp-next"
        className="inline-flex min-w-0 items-center gap-0.5 rounded-full bg-blue-700 px-3 py-1 text-[13px] font-semibold text-white hover:bg-blue-800 disabled:opacity-40"
      >
        {busy === "next" ? <Loader2 size={13} className="animate-spin" /> : null}
        <span className={cx("truncate", compact ? "max-w-[8rem]" : "max-w-[16rem]")}>
          {next ? t("meetpp.header.nextTo", { defaultValue: "Next: {{label}}", label: sectionLabel(next) }) : t("meetpp.header.next", { defaultValue: "Next" })}
        </span>
        <ChevronRight size={14} />
      </button>
      {err && <span className="text-[11px] text-rose-600">{err}</span>}
    </span>
  );
}

function AgentStatus({ compact }: { compact?: boolean }) {
  const { t } = useTranslation();
  const agent = useMeetpp((s) => s.agent);
  const session = useMeetpp((s) => s.snap?.session);
  const speakers = agent?.speakers ?? [];
  const failing = speakers.filter((s) => !s.ok);
  const ai = session?.ai?.status ?? "ok";
  let dot = "bg-emerald-500";
  let text = t("meetpp.status.listening", { defaultValue: "Listening · {{n}} speakers", n: speakers.length });
  if (session?.status === "paused" || agent?.status === "paused") {
    dot = "bg-slate-400";
    text = t("meetpp.status.paused", { defaultValue: "Transcription paused" });
  } else if (agent?.status === "reconnecting" || agent?.status === "offline") {
    dot = "bg-red-600";
    text = t("meetpp.status.reconnecting", { defaultValue: "Transcription reconnecting" });
  } else if (agent?.status === "behind") {
    dot = "bg-amber-500";
    text = t("meetpp.status.behind", { defaultValue: "Behind {{s}} s", s: Math.round(agent.backlog_s ?? 0) });
  } else if (!agent) {
    dot = "bg-slate-300";
    text = t("meetpp.status.starting", { defaultValue: "Starting transcription…" });
  }
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-slate-700">
      <span className="inline-flex items-center gap-1.5">
        <span className={cx("h-2.5 w-2.5 rounded-full", dot)} />
        {!compact && text}
      </span>
      {ai !== "ok" && (
        <span className="inline-flex items-center gap-1 text-amber-700">
          <span className="h-2 w-2 rounded-full bg-amber-500" />
          {t("meetpp.status.aiPaused", { defaultValue: "AI paused — retrying" })}
        </span>
      )}
      {failing.map((s) => (
        <span key={s.identity ?? s.name} className="inline-flex items-center gap-1 text-amber-700" title={t("meetpp.status.speakerHint", { defaultValue: "Audio is received but no text for 20 s; the agent restarts this consumer." })}>
          <span className="h-2 w-2 rounded-full bg-amber-500" />
          {t("meetpp.status.speakerDown", { defaultValue: "{{name}}: no transcript — restarting", name: s.name })}
        </span>
      ))}
    </span>
  );
}

export function FollowChip() {
  const { t } = useTranslation();
  const follow = useMeetpp((s) => s.follow);
  const now = useNow(250, follow.mode === "paused");
  const secs = Math.max(0, Math.ceil((follow.pausedUntil - now) / 1000));
  if (follow.mode === "paused" && secs > 0) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border-2 border-amber-400 bg-amber-50 py-0.5 pl-3 pr-0.5 text-xs font-semibold text-amber-900">
        <Pause size={12} />
        {t("meetpp.follow.paused", { defaultValue: "Auto-follow in {{s}} s", s: secs })}
        <button type="button" onClick={() => resumeFollow()} className="ml-1 rounded-full bg-amber-500 px-2.5 py-0.5 text-white hover:bg-amber-600">
          {t("meetpp.follow.resume", { defaultValue: "Resume now" })}
        </button>
      </span>
    );
  }
  const off = follow.mode === "off";
  return (
    <button
      type="button"
      onClick={() => toggleFollow()}
      aria-pressed={!off}
      title={off ? t("meetpp.follow.turnOn", { defaultValue: "Turn automatic following on" }) : t("meetpp.follow.turnOff", { defaultValue: "Turn automatic following off" })}
      className={cx(
        "inline-flex items-center gap-1 rounded-full border-2 px-3 py-0.5 text-xs font-medium",
        off ? "border-slate-300 bg-slate-100 text-slate-600" : "border-emerald-400 bg-emerald-50 text-emerald-800",
      )}
    >
      {off ? <Play size={11} /> : <span>⟳</span>}
      {off ? t("meetpp.follow.off", { defaultValue: "Follow off" }) : t("meetpp.follow.following", { defaultValue: "Following live" })}
    </button>
  );
}

function TranscriptToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const { t } = useTranslation();
  const count = useMeetpp((s) => s.transcript.length);
  const [seen, setSeen] = useState(count);
  const fresh = open ? 0 : Math.max(0, count - seen);
  return (
    <button
      type="button"
      onClick={() => {
        setSeen(count);
        onToggle();
      }}
      aria-expanded={open}
      className="relative inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700"
    >
      <MessageSquareText size={13} /> {t("meetpp.board.segTranscript", { defaultValue: "Transcript" })}
      {fresh > 0 && <span className="rounded-full bg-blue-600 px-1.5 text-[10px] font-bold text-white">{fresh > 99 ? "99+" : fresh}</span>}
    </button>
  );
}

// ── Chair bar: proposal + AI-move undo (inline, chair and co-hosts) ───────

function ChairBar() {
  const { t } = useTranslation();
  const proposal = useMeetpp((s) => s.proposal);
  const undo = useMeetpp((s) => s.undo);
  const sections = useMeetpp((s) => s.snap?.sections ?? []);
  const now = useNow(500, !!undo);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const undoLeft = undo ? Math.ceil((undo.until - now) / 1000) : 0;
  const showUndo = !!undo && undoLeft > 0;
  if (!proposal && !showUndo) return null;
  const run = async (p: Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await p;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const target = undo ? sections.find((s) => s.id === undo.to) : null;
  return (
    <div className="flex-shrink-0 space-y-1 border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-[13px] text-amber-950">
      {showUndo && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 flex-1">
            {t("meetpp.undo.text", { defaultValue: "AI moved the meeting to {{label}}.", label: sectionLabel(target ?? null) || "…" })}
          </span>
          <button type="button" disabled={busy} onClick={() => void run(meetppActions.undo())} className="inline-flex items-center gap-1 rounded-full bg-amber-600 px-3 py-0.5 text-xs font-semibold text-white hover:bg-amber-700 disabled:opacity-50">
            <RotateCcw size={12} /> {t("meetpp.undo.button", { defaultValue: "Undo ({{s}} s)", s: undoLeft })}
          </button>
        </div>
      )}
      {proposal && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 flex-1">
            <b>{t("meetpp.proposal.suggests", { defaultValue: "AI suggests moving to {{label}}", label: proposal.title })}</b>
            {proposal.reason ? ` — ${proposal.reason}` : ""}
            <span className="ml-1 text-amber-700">({Math.round((proposal.confidence ?? 0) * 100)}%)</span>
          </span>
          <button type="button" disabled={busy} onClick={() => void run(meetppActions.acceptProposal(proposal.pid))} className="rounded-full bg-blue-700 px-3 py-0.5 text-xs font-semibold text-white hover:bg-blue-800 disabled:opacity-50">
            {t("meetpp.proposal.move", { defaultValue: "Move" })}
          </button>
          <button type="button" disabled={busy} onClick={() => void run(meetppActions.rejectProposal(proposal.pid, proposal.to))} className="rounded-full border border-amber-400 bg-white px-3 py-0.5 text-xs font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50">
            {t("meetpp.proposal.notNow", { defaultValue: "Not now" })}
          </button>
        </div>
      )}
      {err && <div className="text-[11px] text-rose-700">{err}</div>}
    </div>
  );
}

function PhoneChairBar() {
  return (
    <div className="flex flex-shrink-0 justify-center border-t border-slate-200 bg-white px-2 py-1.5">
      <NavButtons compact />
    </div>
  );
}

// ── Announcement banner (4 s, over header + outline; transcript visible) ──

function Banner({ coverRight, canChair, onDismiss }: { coverRight: number; canChair: boolean; onDismiss: () => void }) {
  const { t } = useTranslation();
  const a = useMeetpp((s) => s.announcement)!;
  const undo = useMeetpp((s) => s.undo);
  const now = useNow(500, !!undo);
  const undoLeft = undo ? Math.ceil((undo.until - now) / 1000) : 0;
  const [busy, setBusy] = useState(false);
  const icon = useMemo(() => (a.kind === "timebox" ? "⏱" : a.kind === "session" ? "●" : "▶"), [a.kind]);
  return (
    <div
      role="status"
      className="absolute left-0 top-0 z-30 flex min-h-[96px] items-center gap-4 rounded-tl-2xl border-b-4 border-blue-700 bg-blue-900/95 px-6 py-4 text-white shadow-2xl"
      style={{ right: coverRight }}
      onClick={onDismiss}
    >
      <span className="grid h-12 w-12 flex-shrink-0 place-items-center rounded-full bg-blue-600 text-2xl">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-[28px] font-bold leading-tight">{a.title}</div>
        {a.subtitle && <div className="mt-0.5 text-base text-blue-100">{a.subtitle}</div>}
      </div>
      {canChair && undo && undoLeft > 0 && a.kind === "position" && (
        <button
          type="button"
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            setBusy(true);
            void meetppActions.undo().finally(() => setBusy(false));
          }}
          className="inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-amber-500 px-4 py-2 text-sm font-semibold text-white hover:bg-amber-600"
        >
          <RotateCcw size={14} /> {t("meetpp.undo.button", { defaultValue: "Undo ({{s}} s)", s: undoLeft })}
        </button>
      )}
    </div>
  );
}

function Popover({ children, onClose, className }: { children: ReactNode; onClose: () => void; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useOnClickOutside(ref, onClose);
  return (
    <div ref={ref} className={cx("absolute z-30 max-h-[80%] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl", className)}>
      {children}
    </div>
  );
}
