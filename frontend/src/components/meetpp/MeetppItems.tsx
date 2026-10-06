import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { ArrowUpRight, Check, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import {
  clearBadge,
  jumpToTranscript,
  selectItem,
  useMeetpp,
} from "../../lib/meetpp/store";
import { meetppActions } from "../../lib/meetpp/session";
import { meetppApi } from "../../lib/meetpp/api";
import { fmtClock, lowerBound } from "../../lib/meetpp/state";
import type {
  ActionDto,
  AttachmentDto,
  BallotChoice,
  VoteMethod,
  AttendanceStatus,
  AttendeeDto,
  DecisionDto,
  DocumentDto,
  HumanOp,
  MinuteDto,
  SectionDto,
} from "../../lib/meetpp/types";
import { AuthImage, InlineInput, MdView, Pill, cx, type Tone } from "./ui";

/** Shared error reporting for inline edits. */
export function useRun(): [(p: Promise<unknown>) => Promise<boolean>, string | null, () => void] {
  const [error, setError] = useState<string | null>(null);
  const run = async (p: Promise<unknown>) => {
    setError(null);
    try {
      await p;
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    }
  };
  return [run, error, () => setError(null)];
}

function ops(list: HumanOp[]) {
  return meetppActions.ops(list);
}

// ── Badge seen ≥ 1 s (FDD §5.7 Highlight) ──────────────────────────────────

function useSeenBadge(ref: RefObject<HTMLElement>, id: string, hasBadge: boolean): void {
  useEffect(() => {
    const el = ref.current;
    if (!el || !hasBadge || typeof IntersectionObserver === "undefined") return;
    let timer: number | null = null;
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries.some((e) => e.isIntersecting && e.intersectionRatio >= 0.5);
        if (visible && document.visibilityState === "visible") {
          if (timer === null) timer = window.setTimeout(() => clearBadge(id), 1000);
        } else if (timer !== null) {
          window.clearTimeout(timer);
          timer = null;
        }
      },
      { threshold: [0, 0.5, 1] },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [ref, id, hasBadge]);
}

/** Card frame: amber outline while activated, NEW / UPDATED badge, selection. */
export function ItemFrame({
  id,
  kind,
  evidence,
  children,
  dashed,
  className,
}: {
  id: string;
  kind: "decision" | "action";
  evidence: number[];
  children: ReactNode;
  dashed?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const badge = useMeetpp((s) => s.badges[id]);
  const active = useMeetpp((s) => !!s.highlight?.items.includes(id));
  const selected = useMeetpp((s) => s.selected?.id === id);
  useSeenBadge(ref, id, !!badge);
  return (
    <div
      ref={ref}
      data-item={id}
      onClick={() => selectItem(selected ? null : { kind, id, evidence })}
      className={cx(
        "relative cursor-pointer rounded-xl border-2 bg-white px-3 py-2.5 transition-colors",
        active ? "border-amber-400 bg-amber-50/60" : selected ? "border-blue-500" : dashed ? "border-dashed border-amber-300" : "border-slate-200 hover:border-slate-300",
        className,
      )}
    >
      {badge && (
        <span className="absolute right-2 top-2 rounded-full bg-amber-500 px-2 py-0.5 text-[10px] font-bold uppercase text-white">
          {badge === "new" ? t("meetpp.item.new", { defaultValue: "New" }) : t("meetpp.item.updated", { defaultValue: "Updated" })}
        </span>
      )}
      {children}
    </div>
  );
}

/** "AI · 14:15 · Noam G. ↗" — origin, time and speaker of the evidence. */
export function Provenance({ origin, evidence, at, id, kind }: { origin: string; evidence: number[]; at?: string | null; id: string; kind: "decision" | "action" }) {
  const { t } = useTranslation();
  const transcript = useMeetpp((s) => s.transcript);
  const seg = useMemo(() => {
    const seq = evidence?.length ? evidence[evidence.length - 1] : null;
    if (seq === null) return null;
    const i = lowerBound(transcript, seq);
    return i < transcript.length && transcript[i].seq === seq ? transcript[i] : null;
  }, [transcript, evidence]);
  const originLabel =
    origin === "ai"
      ? t("meetpp.item.originAi", { defaultValue: "AI" })
      : origin === "pdf"
        ? t("meetpp.item.originPdf", { defaultValue: "PDF" })
        : t("meetpp.item.originUser", { defaultValue: "Chair" });
  const time = fmtClock(at ?? seg?.t_start ?? null, false);
  return (
    <span className="ml-auto inline-flex items-center gap-1 text-[11px] italic text-slate-500">
      {[originLabel, time, seg?.name].filter(Boolean).join(" · ")}
      {evidence?.length > 0 && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            selectItem({ kind, id, evidence });
            jumpToTranscript(evidence[0]);
          }}
          className="rounded p-0.5 not-italic text-blue-700 hover:bg-blue-50"
          aria-label={t("meetpp.item.evidence", { defaultValue: "Show in transcript" })}
          title={t("meetpp.item.evidence", { defaultValue: "Show in transcript" })}
        >
          <ArrowUpRight size={13} />
        </button>
      )}
    </span>
  );
}

const DECISION_TONE: Record<string, Tone> = { adopted: "green", rejected: "red", withdrawn: "slate", proposed: "blue", pending: "amber" };

function useDecisionStatusLabel() {
  const { t } = useTranslation();
  return (status: string) =>
    ({
      pending: t("meetpp.decision.pending", { defaultValue: "To decide" }),
      proposed: t("meetpp.decision.proposed", { defaultValue: "Proposed" }),
      adopted: t("meetpp.decision.adopted", { defaultValue: "Adopted" }),
      rejected: t("meetpp.decision.rejected", { defaultValue: "Rejected" }),
      withdrawn: t("meetpp.decision.withdrawn", { defaultValue: "Withdrawn" }),
    })[status] ?? status;
}

export function useVoteMethodLabel() {
  const { t } = useTranslation();
  return (m: string) =>
    ({
      voice: t("meetpp.vote.voice", { defaultValue: "Voice vote" }),
      show_of_hands: t("meetpp.vote.showOfHands", { defaultValue: "Show of hands" }),
      assent: t("meetpp.vote.assent", { defaultValue: "Assent" }),
      consensus: t("meetpp.vote.consensus", { defaultValue: "Consensus" }),
      roll_call: t("meetpp.vote.rollCall", { defaultValue: "Roll call" }),
    })[m] ?? m;
}

// ── Decision ──────────────────────────────────────────────────────────────

export function DecisionCard({ d, canEdit, canVote }: { d: DecisionDto; canEdit: boolean; canVote?: boolean }) {
  const { t } = useTranslation();
  const statusLabel = useDecisionStatusLabel();
  const methodLabel = useVoteMethodLabel();
  const formal = useMeetpp((s) => s.snap?.session.meeting_type !== "informal");
  const [editing, setEditing] = useState(false);
  const [voting, setVoting] = useState(false);
  const [run, error] = useRun();
  const pending = d.status === "pending";
  const v = d.vote;
  return (
    <ItemFrame id={d.id} kind="decision" evidence={d.evidence} dashed={pending}>
      <div className="pr-14 text-[14px] font-semibold leading-snug text-slate-900">
        <span className="mr-1 text-slate-500">{d.ref}</span>
        {d.title}
        {d.previous && <span className="ml-2 text-[11px] font-normal text-slate-400">{t("meetpp.decision.previous", { defaultValue: "earlier meeting" })}</span>}
      </div>
      {d.resolution && (
        <div className="mt-1 text-[13px] text-slate-700">
          {d.status === "adopted" && <b className="mr-1">{t("meetpp.decision.resolved", { defaultValue: "RESOLVED:" })}</b>}
          {d.resolution}
        </div>
      )}
      {d.how_taken && <div className="mt-0.5 text-[12px] italic text-slate-500">{d.how_taken}</div>}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <Pill tone={DECISION_TONE[d.status] ?? "slate"}>
          {statusLabel(d.status)}
          {d.decided_at && !pending ? ` · ${fmtClock(d.decided_at, false)}` : ""}
        </Pill>
        {v && (v.for !== null || v.against !== null || v.abstain !== null) && (
          <Pill tone="indigo" title={methodLabel(v.method)}>
            {t("meetpp.vote.summary", { defaultValue: "For {{f}} · Against {{a}} · Abst. {{x}}", f: v.for ?? 0, a: v.against ?? 0, x: v.abstain ?? 0 })}
          </Pill>
        )}
        {v && v.for === null && v.against === null && <Pill tone="indigo">{methodLabel(v.method)}</Pill>}
        {v && !v.confirmed && !pending && <Pill tone="amber">{t("meetpp.vote.toConfirm", { defaultValue: "vote to confirm" })}</Pill>}
        {!d.confirmed && d.origin === "ai" && !pending && <Pill tone="slate">{t("meetpp.item.unconfirmed", { defaultValue: "unconfirmed" })}</Pill>}
        <Provenance origin={d.origin} evidence={d.evidence} at={d.decided_at} id={d.id} kind="decision" />
      </div>
      {canEdit && !editing && (
        <div className="mt-2 flex flex-wrap gap-1.5" onClick={(e) => e.stopPropagation()}>
          {!d.confirmed && !pending && (
            <SmallBtn tone="green" onClick={() => void run(ops([{ op: "decision.confirm", id: d.id }]))}>
              <Check size={12} /> {t("meetpp.item.confirm", { defaultValue: "Confirm" })}
            </SmallBtn>
          )}
          <SmallBtn onClick={() => setEditing(true)}>
            <Pencil size={12} /> {t("meetpp.item.edit", { defaultValue: "Edit" })}
          </SmallBtn>
          {d.status !== "rejected" && (
            <SmallBtn tone="red" onClick={() => void run(ops([{ op: "decision.reject", id: d.id }]))}>
              <X size={12} /> {t("meetpp.item.reject", { defaultValue: "Reject" })}
            </SmallBtn>
          )}
          {canVote && !pending && (
            <SmallBtn onClick={() => setVoting((v) => !v)}>{t("meetpp.vote.edit", { defaultValue: "Vote record" })}</SmallBtn>
          )}
        </div>
      )}
      {voting && (
        <div className="mt-2" onClick={(e) => e.stopPropagation()}>
          <VoteEditor d={d} formal={formal} />
        </div>
      )}
      {editing && (
        <EditForm
          fields={[
            { key: "title", label: t("meetpp.decision.title", { defaultValue: "Title" }), value: d.title },
            { key: "resolution", label: t("meetpp.decision.resolution", { defaultValue: "Resolution (that …)" }), value: d.resolution ?? "", multiline: true },
            { key: "how_taken", label: t("meetpp.decision.howTaken", { defaultValue: "How it was taken" }), value: d.how_taken ?? "" },
            {
              key: "status",
              label: t("meetpp.decision.status", { defaultValue: "Status" }),
              value: d.status,
              options: ["pending", "proposed", "adopted", "rejected", "withdrawn"].map((s) => [s, statusLabel(s)]),
            },
          ]}
          onCancel={() => setEditing(false)}
          onSave={async (changed) => {
            if (Object.keys(changed).length === 0) return setEditing(false);
            if (await run(ops([{ op: "decision.update", id: d.id, ...changed }]))) setEditing(false);
          }}
        />
      )}
      {error && <ErrorLine text={error} />}
    </ItemFrame>
  );
}

// ── Action ────────────────────────────────────────────────────────────────

const ACTION_TONE: Record<string, Tone> = { proposed: "slate", open: "blue", in_progress: "blue", done: "green", cancelled: "red" };

function useActionStatusLabel() {
  const { t } = useTranslation();
  return (status: string) =>
    ({
      proposed: t("meetpp.action.proposed", { defaultValue: "Proposed" }),
      open: t("meetpp.action.open", { defaultValue: "Open" }),
      in_progress: t("meetpp.action.inProgress", { defaultValue: "In progress" }),
      done: t("meetpp.action.done", { defaultValue: "Done" }),
      cancelled: t("meetpp.action.cancelled", { defaultValue: "Cancelled" }),
    })[status] ?? status;
}

export function ActionCard({ a, canEdit }: { a: ActionDto; canEdit: boolean }) {
  const { t } = useTranslation();
  const statusLabel = useActionStatusLabel();
  const [editing, setEditing] = useState(false);
  const [run, error] = useRun();
  const toReview = a.previous && !a.report;
  return (
    <ItemFrame id={a.id} kind="action" evidence={a.evidence} dashed={toReview}>
      <div className="pr-14 text-[14px] font-semibold leading-snug text-slate-900">
        <span className="mr-1 text-slate-500">{a.ref}</span>
        {a.title}
      </div>
      {a.description && <div className="mt-0.5 text-[13px] text-slate-700">{a.description}</div>}
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-slate-600">
        <span>
          {t("meetpp.action.assignees", { defaultValue: "Assignees" })}:{" "}
          <b className="font-medium text-slate-800">{a.assignees?.length ? a.assignees.map((x) => x.name).join(", ") : t("meetpp.action.unassigned", { defaultValue: "unassigned" })}</b>
        </span>
        {a.due && (
          <span>
            {t("meetpp.action.due", { defaultValue: "Due" })}: <b className="font-medium text-slate-800">{a.due}</b>
          </span>
        )}
        {a.decision_ref && <span>{t("meetpp.action.fromDecision", { defaultValue: "From {{ref}}", ref: a.decision_ref })}</span>}
      </div>
      {a.report && (
        <div className="mt-1.5 rounded-lg bg-slate-50 px-2 py-1 text-[12px] text-slate-700">
          <b className="mr-1">{t("meetpp.action.reported", { defaultValue: "Reported at this meeting:" })}</b>
          {a.report.note}
        </div>
      )}
      {a.progress_notes && <div className="mt-1 text-[12px] italic text-slate-500">{a.progress_notes}</div>}
      {a.completion_note && <div className="mt-1 text-[12px] text-emerald-700">{a.completion_note}</div>}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {toReview ? (
          <Pill tone="amber">{t("meetpp.action.toReview", { defaultValue: "To review" })}</Pill>
        ) : (
          <Pill tone={ACTION_TONE[a.status] ?? "slate"}>{statusLabel(a.status)}</Pill>
        )}
        {toReview && <Pill tone={ACTION_TONE[a.status] ?? "slate"}>{statusLabel(a.status)}</Pill>}
        {a.carried_forward && <Pill tone="indigo">{t("meetpp.action.carried", { defaultValue: "carried forward" })}</Pill>}
        <Provenance origin={a.origin} evidence={a.evidence} at={a.report?.at ?? null} id={a.id} kind="action" />
      </div>
      {canEdit && !editing && (
        <div className="mt-2 flex flex-wrap gap-1.5" onClick={(e) => e.stopPropagation()}>
          {a.status === "proposed" && (
            <SmallBtn tone="green" onClick={() => void run(ops([{ op: "action.confirm", id: a.id }]))}>
              <Check size={12} /> {t("meetpp.item.confirm", { defaultValue: "Confirm" })}
            </SmallBtn>
          )}
          <SmallBtn onClick={() => setEditing(true)}>
            <Pencil size={12} /> {t("meetpp.item.edit", { defaultValue: "Edit" })}
          </SmallBtn>
          {a.status !== "cancelled" && !a.previous && (
            <SmallBtn tone="red" onClick={() => void run(ops([{ op: "action.reject", id: a.id }]))}>
              <X size={12} /> {t("meetpp.item.reject", { defaultValue: "Reject" })}
            </SmallBtn>
          )}
        </div>
      )}
      {editing && (
        <EditForm
          fields={[
            { key: "title", label: t("meetpp.action.title", { defaultValue: "Title" }), value: a.title },
            { key: "description", label: t("meetpp.action.description", { defaultValue: "Description" }), value: a.description ?? "", multiline: true },
            { key: "assignees", label: t("meetpp.action.assigneesHint", { defaultValue: "Assignees (comma-separated)" }), value: (a.assignees ?? []).map((x) => x.name).join(", ") },
            { key: "due", label: t("meetpp.action.due", { defaultValue: "Due" }), value: a.due ?? "", type: "date" },
            {
              key: "status",
              label: t("meetpp.action.status", { defaultValue: "Status" }),
              value: a.status,
              options: ["proposed", "open", "in_progress", "done", "cancelled"].map((s) => [s, statusLabel(s)]),
            },
            ...(a.previous ? [{ key: "report_note", label: t("meetpp.action.reportNote", { defaultValue: "Reported at this meeting" }), value: a.report?.note ?? "", multiline: true }] : []),
          ]}
          onCancel={() => setEditing(false)}
          onSave={async (changed) => {
            const patch: Record<string, unknown> = { ...changed };
            if ("assignees" in patch) patch.assignees = String(patch.assignees).split(",").map((x) => x.trim()).filter(Boolean);
            if ("due" in patch && !patch.due) patch.due = null;
            if (Object.keys(patch).length === 0) return setEditing(false);
            if (await run(ops([{ op: "action.update", id: a.id, ...patch }]))) setEditing(false);
          }}
        />
      )}
      {error && <ErrorLine text={error} />}
    </ItemFrame>
  );
}

// ── Manual add (decision / action) ────────────────────────────────────────

export function AddItem({ kind, sectionId }: { kind: "decision" | "action"; sectionId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [run, error] = useRun();
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-blue-700 hover:bg-blue-50">
        <Plus size={12} />
        {kind === "decision" ? t("meetpp.decision.add", { defaultValue: "Add decision" }) : t("meetpp.action.add", { defaultValue: "Add action" })}
      </button>
    );
  }
  const fields =
    kind === "decision"
      ? [
          { key: "title", label: t("meetpp.decision.title", { defaultValue: "Title" }), value: "" },
          { key: "resolution", label: t("meetpp.decision.resolution", { defaultValue: "Resolution (that …)" }), value: "", multiline: true },
        ]
      : [
          { key: "title", label: t("meetpp.action.title", { defaultValue: "Title" }), value: "" },
          { key: "assignees", label: t("meetpp.action.assigneesHint", { defaultValue: "Assignees (comma-separated)" }), value: "" },
          { key: "due", label: t("meetpp.action.due", { defaultValue: "Due" }), value: "", type: "date" as const },
        ];
  return (
    <div className="rounded-xl border border-dashed border-blue-300 bg-blue-50/40 p-2">
      <EditForm
        fields={fields}
        onCancel={() => setOpen(false)}
        onSave={async (v) => {
          if (!v.title?.trim()) return;
          const op: HumanOp =
            kind === "decision"
              ? { op: "decision.add", section_id: sectionId, title: v.title.trim(), ...(v.resolution ? { resolution: v.resolution } : {}) }
              : {
                  op: "action.add",
                  section_id: sectionId,
                  title: v.title.trim(),
                  ...(v.assignees ? { assignees: v.assignees.split(",").map((x) => x.trim()).filter(Boolean) } : {}),
                  ...(v.due ? { due: v.due } : {}),
                };
          if (await run(ops([op]))) setOpen(false);
        }}
        allFields
      />
      {error && <ErrorLine text={error} />}
    </div>
  );
}

// ── Minutes ───────────────────────────────────────────────────────────────

export function MinutesBlock({ m, section, isLive, canChair, canEdit }: { m: MinuteDto | null; section: SectionDto | null; isLive: boolean; canChair: boolean; canEdit: boolean }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [run, error] = useRun();
  const notes = m?.notes ?? [];
  const narrative = m?.narrative_md ?? "";
  const status = m?.status ?? "notes";
  const tierLabel =
    m?.source_tier === "refined"
      ? t("meetpp.minutes.tierRefined", { defaultValue: "refined transcript" })
      : m?.source_tier === "live"
        ? t("meetpp.minutes.tierLive", { defaultValue: "live transcript" })
        : m?.source_tier === "mixed"
          ? t("meetpp.minutes.tierMixed", { defaultValue: "mixed transcript" })
          : "";
  const statusLine =
    status === "composing"
      ? t("meetpp.minutes.composing", { defaultValue: "composing…" })
      : status === "failed"
        ? t("meetpp.minutes.failed", { defaultValue: "Composition failed" })
        : status === "edited"
          ? t("meetpp.minutes.edited", { defaultValue: "Edited (locked) · v{{v}}", v: m?.version ?? 1 })
          : status === "composed"
            ? t("meetpp.minutes.composed", { defaultValue: "Composed v{{v}} · {{at}}", v: m?.version ?? 1, at: fmtClock(m?.composed_at, false) })
            : "";
  const showRunning = isLive || !narrative || showNotes || status === "failed" || status === "composing";
  return (
    <div className="space-y-2" onClick={(e) => e.stopPropagation()}>
      {(statusLine || canChair || canEdit) && !isLive && (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
          {statusLine && <span className={cx(status === "failed" && "font-semibold text-rose-600", status === "composing" && "italic")}>{statusLine}</span>}
          {tierLabel && <span>· {tierLabel}</span>}
          <span className="flex-1" />
          {canChair && section && (
            <SmallBtn onClick={() => void run(meetppActions.compose(section.id))}>
              <RefreshCw size={11} /> {status === "failed" ? t("meetpp.minutes.retry", { defaultValue: "Retry" }) : narrative ? t("meetpp.minutes.regenerate", { defaultValue: "Regenerate" }) : t("meetpp.minutes.composeNow", { defaultValue: "Compose now" })}
            </SmallBtn>
          )}
          {canEdit && !editing && (
            <SmallBtn onClick={() => setEditing(true)}>
              <Pencil size={11} /> {t("meetpp.item.edit", { defaultValue: "Edit" })}
            </SmallBtn>
          )}
        </div>
      )}
      {editing ? (
        <div>
          <InlineInput
            multiline
            value={narrative}
            ariaLabel={t("meetpp.minutes.editLabel", { defaultValue: "Minutes (Markdown)" })}
            onCancel={() => setEditing(false)}
            onSave={async (v) => {
              const op: HumanOp = section ? { op: "minutes.edit", section_id: section.id, narrative_md: v } : { op: "minutes.edit", kind: m?.kind, narrative_md: v };
              if (await run(meetppActions.ops([op]))) setEditing(false);
            }}
          />
          <div className="mt-1 text-[10px] text-slate-400">{t("meetpp.minutes.editHint", { defaultValue: "Ctrl+Enter saves · Esc cancels · saving locks this part" })}</div>
        </div>
      ) : (
        narrative && !isLive && <MdView text={narrative} />
      )}
      {showRunning && (
        <div>
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            {t("meetpp.minutes.running", { defaultValue: "Running notes" })}
          </div>
          {notes.length === 0 ? (
            <div className="text-[12px] italic text-slate-400">{t("meetpp.minutes.noNotes", { defaultValue: "No notes yet." })}</div>
          ) : (
            <ul className="list-disc space-y-0.5 pl-5 text-[13px] text-slate-700">
              {notes.map((n, i) => (
                <li key={i}>
                  {n.text}
                  {n.evidence?.length > 0 && (
                    <button
                      type="button"
                      className="ml-1 rounded p-0.5 align-middle text-blue-700 hover:bg-blue-50"
                      onClick={() => {
                        selectItem({ kind: "note", id: `${m?.id}:${i}`, evidence: n.evidence });
                        jumpToTranscript(n.evidence[0]);
                      }}
                      aria-label={t("meetpp.item.evidence", { defaultValue: "Show in transcript" })}
                    >
                      <ArrowUpRight size={12} />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {narrative && !isLive && notes.length > 0 && (
        <button type="button" className="text-[11px] text-blue-700 hover:underline" onClick={() => setShowNotes((v) => !v)}>
          {showNotes ? t("meetpp.minutes.hideNotes", { defaultValue: "Hide running notes" }) : t("meetpp.minutes.showNotes", { defaultValue: "Show running notes" })}
        </button>
      )}
      {m?.error && status === "failed" && <div className="text-[11px] text-rose-600">{m.error}</div>}
      {error && <ErrorLine text={error} />}
    </div>
  );
}

// ── Attendance ────────────────────────────────────────────────────────────

export function useAttendanceLabel() {
  const { t } = useTranslation();
  return (s: string) =>
    ({
      present: t("meetpp.attendance.present", { defaultValue: "Present" }),
      represented: t("meetpp.attendance.represented", { defaultValue: "Represented" }),
      absent: t("meetpp.attendance.absent", { defaultValue: "Absent" }),
      excused: t("meetpp.attendance.excused", { defaultValue: "Excused" }),
      not_registered: t("meetpp.attendance.notRegistered", { defaultValue: "Expected" }),
    })[s] ?? s;
}

const ATTENDANCE: AttendanceStatus[] = ["present", "represented", "absent", "excused", "not_registered"];

export function AttendanceTable({ attendees, canEdit, onPatch }: { attendees: AttendeeDto[]; canEdit: boolean; onPatch?: (id: string, patch: Record<string, unknown>) => Promise<unknown> }) {
  const { t } = useTranslation();
  const label = useAttendanceLabel();
  const [run, error] = useRun();
  const badges = useMeetpp((s) => s.badges);
  const highlight = useMeetpp((s) => s.highlight);
  const patch = (id: string, p: Record<string, unknown>) => void run((onPatch ?? meetppActions.patchAttendee)(id, p as never));
  if (attendees.length === 0) {
    return <div className="py-6 text-center text-sm text-slate-400">{t("meetpp.attendance.empty", { defaultValue: "Attendance appears as people join." })}</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse text-left text-[13px]">
        <thead className="text-[11px] uppercase text-slate-500">
          <tr className="border-b border-slate-200">
            <th className="py-1.5 pr-2 font-semibold">{t("meetpp.attendance.name", { defaultValue: "Name" })}</th>
            <th className="pr-2 font-semibold">{t("meetpp.attendance.status", { defaultValue: "Status" })}</th>
            <th className="pr-2 font-semibold">{t("meetpp.attendance.voting", { defaultValue: "Voting" })}</th>
            <th className="pr-2 font-semibold">{t("meetpp.attendance.representedBy", { defaultValue: "Represented by" })}</th>
            <th className="pr-2 font-semibold">{t("meetpp.attendance.transcribed", { defaultValue: "Transcribed" })}</th>
            <th className="pr-2 font-semibold">{t("meetpp.attendance.requiredNext", { defaultValue: "Required next" })}</th>
          </tr>
        </thead>
        <tbody>
          {attendees.map((a) => {
            const hot = highlight?.items.includes(a.id);
            return (
              <tr key={a.id} data-item={a.id} className={cx("border-b border-slate-100 align-top", hot && "bg-amber-50 outline outline-2 outline-amber-400")}>
                <td className="py-1.5 pr-2">
                  <div className="flex items-center gap-1.5">
                    <span className={cx("h-2 w-2 flex-shrink-0 rounded-full", a.online ? "bg-emerald-500" : "bg-slate-300")} title={a.online ? t("meetpp.attendance.online", { defaultValue: "In the meeting" }) : t("meetpp.attendance.offline", { defaultValue: "Not connected" })} />
                    <span className="font-medium text-slate-800">{a.name}</span>
                    {badges[a.id] && <span className="rounded-full bg-amber-500 px-1.5 text-[9px] font-bold uppercase text-white">{badges[a.id] === "new" ? t("meetpp.item.new", { defaultValue: "New" }) : t("meetpp.item.updated", { defaultValue: "Updated" })}</span>}
                  </div>
                  {a.username && <div className="pl-3.5 text-[11px] text-slate-400">{a.username}</div>}
                </td>
                <td className="pr-2">
                  {canEdit ? (
                    <select value={a.status} onChange={(e) => patch(a.id, { status: e.target.value })} className="rounded border border-slate-300 bg-white px-1 py-0.5 text-[12px] text-slate-800" aria-label={t("meetpp.attendance.status", { defaultValue: "Status" })}>
                      {ATTENDANCE.map((s) => (
                        <option key={s} value={s}>
                          {label(s)}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <Pill tone={a.status === "present" ? "green" : a.status === "represented" ? "blue" : a.status === "not_registered" ? "amber" : "slate"}>{label(a.status)}</Pill>
                  )}
                </td>
                <td className="pr-2">
                  {canEdit ? (
                    <input type="checkbox" checked={a.voting} onChange={(e) => patch(a.id, { voting: e.target.checked })} aria-label={t("meetpp.attendance.voting", { defaultValue: "Voting" })} />
                  ) : a.voting ? (
                    "✓"
                  ) : (
                    ""
                  )}
                </td>
                <td className="pr-2">
                  {canEdit && a.status === "represented" ? (
                    <div className="space-y-1">
                      <BlurInput value={a.represented_by ?? ""} placeholder={t("meetpp.attendance.proxy", { defaultValue: "Proxy" })} onCommit={(v) => patch(a.id, { represented_by: v || null })} />
                      <BlurInput value={a.mandate_ref ?? ""} placeholder={t("meetpp.attendance.mandate", { defaultValue: "Mandate ref." })} onCommit={(v) => patch(a.id, { mandate_ref: v || null })} />
                    </div>
                  ) : (
                    <span className="text-slate-600">
                      {a.represented_by ?? ""}
                      {a.mandate_ref ? ` (${a.mandate_ref})` : ""}
                    </span>
                  )}
                </td>
                <td className="pr-2 text-slate-600">{a.opted_out ? t("meetpp.attendance.optedOut", { defaultValue: "No (opted out)" }) : t("meetpp.attendance.yes", { defaultValue: "Yes" })}</td>
                <td className="pr-2">
                  {canEdit ? (
                    <div className="space-y-1">
                      <input type="checkbox" checked={a.required_next} onChange={(e) => patch(a.id, { required_next: e.target.checked })} aria-label={t("meetpp.attendance.requiredNext", { defaultValue: "Required next" })} />
                      {a.required_next && <BlurInput value={a.required_reason ?? ""} placeholder={t("meetpp.attendance.reason", { defaultValue: "Reason" })} onCommit={(v) => patch(a.id, { required_reason: v || null })} />}
                    </div>
                  ) : a.required_next ? (
                    <span className="text-slate-600">✓ {a.required_reason ?? ""}</span>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {error && <ErrorLine text={error} />}
    </div>
  );
}

function BlurInput({ value, placeholder, onCommit }: { value: string; placeholder: string; onCommit: (v: string) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <input
      value={v}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => v !== value && onCommit(v.trim())}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      className="w-full min-w-[7rem] rounded border border-slate-300 px-1.5 py-0.5 text-[12px] text-slate-800"
    />
  );
}

// ── Papers ────────────────────────────────────────────────────────────────

export function DocumentRow({ doc }: { doc: DocumentDto }) {
  const { t } = useTranslation();
  const kind = doc.kind === "agenda" ? t("meetpp.papers.agendaPdf", { defaultValue: "Agenda" }) : t("meetpp.papers.notesPdf", { defaultValue: "Previous meeting notes" });
  return (
    <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[13px]">
      <span className="rounded bg-rose-100 px-1.5 py-0.5 text-[10px] font-bold text-rose-700">PDF</span>
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium text-slate-800">{doc.title || doc.filename}</div>
        <div className="text-[11px] text-slate-500">
          {kind} · {doc.filename}
          {doc.page_count ? ` · ${t("meetpp.papers.pages", { defaultValue: "{{n}} pages", n: doc.page_count })}` : ""}
        </div>
      </div>
      {doc.status !== "done" && (
        <Pill tone={doc.status === "failed" ? "red" : "amber"}>
          {doc.status === "failed" ? t("meetpp.papers.failed", { defaultValue: "could not be read" }) : t("meetpp.papers.reading", { defaultValue: "reading…" })}
        </Pill>
      )}
    </div>
  );
}

export function AttachmentCard({ att, canEdit }: { att: AttachmentDto; canEdit: boolean }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [run, error] = useRun();
  return (
    <div data-item={att.id} className="w-56 overflow-hidden rounded-lg border border-slate-200 bg-white" onClick={(e) => e.stopPropagation()}>
      <AuthImage url={att.url} alt={att.caption || att.filename} className="aspect-video w-full bg-slate-900 object-contain" />
      <div className="px-2 py-1.5">
        {editing ? (
          <InlineInput
            value={att.caption ?? ""}
            ariaLabel={t("meetpp.papers.caption", { defaultValue: "Caption" })}
            onCancel={() => setEditing(false)}
            onSave={async (v) => {
              if (await run(meetppActions.patchAttachment(att.id, v.trim()))) setEditing(false);
            }}
          />
        ) : (
          <div className="truncate text-[12px] text-slate-700" title={att.caption ?? att.filename}>
            {att.caption || att.filename}
          </div>
        )}
        <div className="flex items-center gap-1 text-[10px] text-slate-400">
          {[att.author, fmtClock(att.created_at, false)].filter(Boolean).join(" · ")}
          <span className="flex-1" />
          {canEdit && !editing && (
            <>
              <button type="button" className="rounded p-0.5 hover:bg-slate-100" onClick={() => setEditing(true)} aria-label={t("meetpp.papers.rename", { defaultValue: "Rename" })}>
                <Pencil size={11} />
              </button>
              <button
                type="button"
                className="rounded p-0.5 text-rose-600 hover:bg-rose-50"
                onClick={() => {
                  if (window.confirm(t("meetpp.papers.deleteConfirm", { defaultValue: "Delete this snapshot?" }))) void run(meetppActions.deleteAttachment(att.id));
                }}
                aria-label={t("meetpp.papers.delete", { defaultValue: "Delete" })}
              >
                <Trash2 size={11} />
              </button>
            </>
          )}
        </div>
        {error && <ErrorLine text={error} />}
      </div>
    </div>
  );
}

// ── Small building blocks ─────────────────────────────────────────────────

export function SmallBtn({ children, onClick, tone = "slate", disabled }: { children: ReactNode; onClick: () => void; tone?: "slate" | "green" | "red" | "blue"; disabled?: boolean }) {
  const tones = {
    slate: "border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
    green: "border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100",
    red: "border-rose-200 bg-white text-rose-700 hover:bg-rose-50",
    blue: "border-blue-600 bg-blue-600 text-white hover:bg-blue-700",
  };
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={cx("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium disabled:opacity-50", tones[tone])}
    >
      {children}
    </button>
  );
}

export function ErrorLine({ text }: { text: string }) {
  return <div className="mt-1 rounded bg-rose-50 px-2 py-1 text-[11px] text-rose-700">{text}</div>;
}

interface FieldSpec {
  key: string;
  label: string;
  value: string;
  multiline?: boolean;
  type?: "text" | "date" | "number";
  options?: Array<[string, string]>;
}

/** Inline edit form; onSave receives only the changed fields (or all fields
 * with `allFields`). */
export function EditForm({ fields, onSave, onCancel, allFields }: { fields: FieldSpec[]; onSave: (changed: Record<string, string>) => void | Promise<void>; onCancel: () => void; allFields?: boolean }) {
  const { t } = useTranslation();
  const [vals, setVals] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f.key, f.value])));
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    const out: Record<string, string> = {};
    for (const f of fields) if (allFields || vals[f.key] !== f.value) out[f.key] = vals[f.key];
    setBusy(true);
    try {
      await onSave(out);
    } finally {
      setBusy(false);
    }
  };
  const input = "w-full rounded border border-slate-300 bg-white px-2 py-1 text-[13px] text-slate-800 focus:border-blue-400 focus:outline-none";
  return (
    <form
      className="mt-2 space-y-1.5"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }}
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      {fields.map((f, i) => (
        <label key={f.key} className="block">
          <span className="text-[11px] font-medium text-slate-500">{f.label}</span>
          {f.options ? (
            <select className={input} value={vals[f.key]} onChange={(e) => setVals((v) => ({ ...v, [f.key]: e.target.value }))}>
              {f.options.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          ) : f.multiline ? (
            <textarea rows={3} className={input} value={vals[f.key]} onChange={(e) => setVals((v) => ({ ...v, [f.key]: e.target.value }))} />
          ) : (
            <input autoFocus={i === 0} type={f.type ?? "text"} className={input} value={vals[f.key]} onChange={(e) => setVals((v) => ({ ...v, [f.key]: e.target.value }))} />
          )}
        </label>
      ))}
      <div className="flex justify-end gap-1.5 pt-1">
        <button type="button" onClick={onCancel} className="rounded-md border border-slate-300 px-2.5 py-1 text-[12px] text-slate-700 hover:bg-slate-50">
          {t("meetpp.common.cancel", { defaultValue: "Cancel" })}
        </button>
        <button type="submit" disabled={busy} className="rounded-md bg-blue-600 px-2.5 py-1 text-[12px] font-semibold text-white hover:bg-blue-700 disabled:opacity-60">
          {t("meetpp.common.save", { defaultValue: "Save" })}
        </button>
      </div>
    </form>
  );
}

// ── Vote record editor ────────────────────────────────────────────────────

/** Vote record editor (method, tallies, per-member ballots for formal
 * meetings, confirmation) → PUT /meetpp/decisions/{id}/vote (chair). */
export function VoteEditor({ d, formal }: { d: DecisionDto; formal: boolean }) {
  const { t } = useTranslation();
  const methodLabel = useVoteMethodLabel();
  const attendees = useMeetpp((s) => s.snap?.attendees ?? []);
  const v = d.vote;
  const voters = useMemo(() => attendees.filter((a) => a.voting), [attendees]);
  const initialBallots = useMemo(() => {
    const out = voters.map((a) => {
      const b = v?.ballots?.find((x) => (x.person_key && x.person_key === a.person_key) || x.name === a.name);
      return { name: a.name, person_key: a.person_key, choice: (b?.choice ?? "not_recorded") as BallotChoice, cast_by: b?.cast_by ?? "", proxy: b?.proxy ?? false };
    });
    for (const b of v?.ballots ?? []) {
      if (!out.some((o) => (b.person_key && o.person_key === b.person_key) || o.name === b.name)) {
        out.push({ name: b.name, person_key: b.person_key ?? "", choice: b.choice, cast_by: b.cast_by ?? "", proxy: b.proxy });
      }
    }
    return out;
  }, [voters, v]);
  const [method, setMethod] = useState<VoteMethod>(v?.method ?? "assent");
  const [fr, setFr] = useState<string>(v?.for != null ? String(v.for) : "");
  const [ag, setAg] = useState<string>(v?.against != null ? String(v.against) : "");
  const [ab, setAb] = useState<string>(v?.abstain != null ? String(v.abstain) : "");
  const [ballots, setBallotsState] = useState(initialBallots);
  // Once the chair edits a ballot, refreshed snapshots (every few seconds)
  // must not reset the form; until then it follows the record.
  const [touched, setTouched] = useState(false);
  const setBallots: typeof setBallotsState = (next) => {
    setTouched(true);
    setBallotsState(next);
  };
  const [confirmed, setConfirmed] = useState<boolean>(v?.confirmed ?? false);
  const [run, error] = useRun();
  const [saved, setSaved] = useState(false);
  const ballotsKey = JSON.stringify(initialBallots);
  useEffect(() => {
    if (!touched) setBallotsState(initialBallots);
    // Compared by content: every snapshot builds new arrays.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ballotsKey, touched]);

  const count = () => {
    setFr(String(ballots.filter((b) => b.choice === "for").length));
    setAg(String(ballots.filter((b) => b.choice === "against").length));
    setAb(String(ballots.filter((b) => b.choice === "abstain").length));
  };
  const num = (s: string) => (s.trim() === "" ? null : Math.max(0, Math.round(Number(s))));
  const save = async () => {
    setSaved(false);
    const ok = await run(
      (async () => {
        const res = await meetppApi.putVote(d.id, {
          method,
          for: num(fr),
          against: num(ag),
          abstain: num(ab),
          ...(formal ? { ballots: ballots.map((b) => ({ name: b.name, person_key: b.person_key || null, choice: b.choice, cast_by: b.cast_by || null, proxy: b.proxy })) } : {}),
          confirmed,
        });
        useMeetpp.setState((s) => (s.snap ? { snap: { ...s.snap, decisions: s.snap.decisions.map((x) => (x.id === d.id ? { ...x, ...res } : x)) } } : {}));
      })(),
    );
    setSaved(ok);
    // Saved: follow the record again.
    if (ok) setTouched(false);
  };
  const input = "w-20 rounded border border-slate-300 px-2 py-1 text-sm text-slate-800";
  return (
    <div className="rounded-xl bg-slate-50 p-3 text-sm">
      <div className="mb-2 font-semibold text-slate-800">{t("meetpp.vote.title", { defaultValue: "Vote record" })}</div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-xs text-slate-500">{t("meetpp.vote.method", { defaultValue: "Method" })}</span>
          <select className="block rounded border border-slate-300 px-2 py-1 text-sm text-slate-800" value={method} onChange={(e) => setMethod(e.target.value as VoteMethod)}>
            {(["voice", "show_of_hands", "assent", "consensus", "roll_call"] as VoteMethod[]).map((m) => (
              <option key={m} value={m}>
                {methodLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">{t("meetpp.vote.for", { defaultValue: "For" })}</span>
          <input className={cx(input, "block")} type="number" min={0} value={fr} onChange={(e) => setFr(e.target.value)} />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">{t("meetpp.vote.against", { defaultValue: "Against" })}</span>
          <input className={cx(input, "block")} type="number" min={0} value={ag} onChange={(e) => setAg(e.target.value)} />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">{t("meetpp.vote.abstain", { defaultValue: "Abstain" })}</span>
          <input className={cx(input, "block")} type="number" min={0} value={ab} onChange={(e) => setAb(e.target.value)} />
        </label>
        {v && (
          <div className="text-xs text-slate-600">
            {t("meetpp.vote.quorumLine", {
              defaultValue: "Eligible {{e}} · present or represented {{p}} · quorum {{q}} {{met}} · result: {{r}}",
              e: v.eligible ?? "—",
              p: v.present ?? "—",
              q: v.quorum_required ?? "—",
              met: v.quorum_met === null ? "" : v.quorum_met ? "✓" : "✕",
              r: v.result ?? "—",
            })}
          </div>
        )}
      </div>
      {formal && ballots.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[480px] text-left text-sm">
            <thead className="text-xs uppercase text-slate-500">
              <tr>
                <th className="py-1">{t("meetpp.vote.member", { defaultValue: "Member" })}</th>
                <th>{t("meetpp.vote.choice", { defaultValue: "Vote" })}</th>
                <th>{t("meetpp.vote.castBy", { defaultValue: "Cast by" })}</th>
                <th>{t("meetpp.vote.proxy", { defaultValue: "Proxy" })}</th>
              </tr>
            </thead>
            <tbody>
              {ballots.map((b, i) => (
                <tr key={`${b.person_key}-${b.name}`} className="border-t border-slate-200">
                  <td className="py-1 pr-2">{b.name}</td>
                  <td className="pr-2">
                    <select
                      className="rounded border border-slate-300 px-1 py-0.5 text-sm text-slate-800"
                      value={b.choice}
                      onChange={(e) => setBallots((list) => list.map((x, j) => (j === i ? { ...x, choice: e.target.value as BallotChoice } : x)))}
                    >
                      <option value="for">{t("meetpp.vote.for", { defaultValue: "For" })}</option>
                      <option value="against">{t("meetpp.vote.against", { defaultValue: "Against" })}</option>
                      <option value="abstain">{t("meetpp.vote.abstain", { defaultValue: "Abstain" })}</option>
                      <option value="not_recorded">{t("meetpp.vote.notRecorded", { defaultValue: "Not recorded" })}</option>
                    </select>
                  </td>
                  <td className="pr-2">
                    <input className="w-36 rounded border border-slate-300 px-1.5 py-0.5 text-sm text-slate-800" value={b.cast_by} onChange={(e) => setBallots((list) => list.map((x, j) => (j === i ? { ...x, cast_by: e.target.value } : x)))} />
                  </td>
                  <td>
                    <input type="checkbox" checked={b.proxy} onChange={(e) => setBallots((list) => list.map((x, j) => (j === i ? { ...x, proxy: e.target.checked } : x)))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button type="button" onClick={count} className="mt-1 text-xs font-medium text-blue-700 hover:underline">
            {t("meetpp.vote.count", { defaultValue: "Count the ballots into the tallies" })}
          </button>
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <label className="inline-flex items-center gap-1.5">
          <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
          {t("meetpp.vote.confirm", { defaultValue: "I confirm this vote record" })}
        </label>
        <span className="flex-1" />
        {saved && (
          <span className="inline-flex items-center gap-1 text-xs text-emerald-700">
            <Check size={12} /> {t("meetpp.review.savedShort", { defaultValue: "Saved" })}
          </span>
        )}
        <button type="button" onClick={() => void save()} className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-700">
          {t("meetpp.vote.save", { defaultValue: "Save vote" })}
        </button>
      </div>
      {error && <ErrorLine text={error} />}
    </div>
  );
}
