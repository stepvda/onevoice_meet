import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Download, ExternalLink, Loader2, Plus, RefreshCw, Send, Trash2, X } from "lucide-react";
import { MeetppApiError, meetppApi, setMeetppRoomToken } from "../lib/meetpp/api";
import { refetchState, syncTranscript } from "../lib/meetpp/session";
import { applySnapshot, setCtx, useMeetpp } from "../lib/meetpp/store";
import { fmtClock, outlineOrder, sectionLabel } from "../lib/meetpp/state";
import type {
  JobInfo,
  OutputDto,
  Recipient,
  ReviewDraft,
  ReviewResponse,
  SectionDto,
} from "../lib/meetpp/types";
import { loadPendingToken } from "./Lobby";
import {
  ActionCard,
  AttachmentCard,
  AttendanceTable,
  DecisionCard,
  DocumentRow,
  ErrorLine,
  MinutesBlock,
  VoteEditor,
} from "../components/meetpp/MeetppItems";
import MeetppTranscript from "../components/meetpp/MeetppTranscript";
import { Pill, cx } from "../components/meetpp/ui";

/**
 * Meet++ review (FDD §5.13, Figure 6): left navigation by report section,
 * job status + retry, Preview PDF (the real report), the vote editor per
 * decision, per-section minutes with status / Regenerate / Edit, next
 * meeting and distribution, and Publish & send. Nothing is sent before the
 * Publish click.
 */

type Sec = "report" | "attendance" | "agenda" | "decisions" | "actions" | "papers" | "minutes" | "transcript" | "next" | "distribution";

const DEFAULT_DRAFT: ReviewDraft = {
  next_meeting: { room: "same" },
  next_agenda: [],
  recipients: { report: [], invite: [] },
  distribution: { send_report: true, send_invites: true, attach_snapshots: true, include_transcript: false },
};

function useWide(min = 1280): boolean {
  const q = `(min-width: ${min}px)`;
  const [wide, setWide] = useState(() => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(q).matches : true));
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(q);
    const on = () => setWide(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, [q]);
  return wide;
}

export default function MeetppReview() {
  const { sessionId = "" } = useParams();
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [draft, setDraft] = useState<ReviewDraft | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sec, setSec] = useState<Sec>("report");
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [published, setPublished] = useState<{ published_at: string; outputs: OutputDto[]; email_results: Array<Record<string, unknown>> } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const wide = useWide();
  const draftDirty = useRef(false);
  const rightRef = useRef<HTMLDivElement>(null);

  // Room token (if this tab still has one) + review context for the store.
  useEffect(() => {
    setMeetppRoomToken(loadPendingToken()?.token ?? null);
    setCtx({ mode: "review", isChair: true, roomName: null, localIdentity: null, localName: null });
  }, []);

  const loadReview = useCallback(async () => {
    try {
      const r = await meetppApi.getReview(sessionId);
      setReview(r);
      setLoadError(null);
      return r;
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [sessionId]);

  const loadState = useCallback(async () => {
    try {
      const s = await meetppApi.getState(sessionId);
      applySnapshot(s);
      setStateError(null);
      void syncTranscript(0, true);
    } catch (e) {
      setStateError(e instanceof MeetppApiError ? `${e.message} (${e.status})` : String(e));
    }
  }, [sessionId]);

  useEffect(() => {
    void (async () => {
      const r = await loadReview();
      await loadState();
      if (r && !draftDirty.current) {
        const s = useMeetpp.getState().snap;
        setDraft(initialDraft(r, s?.attendees ?? []));
      }
    })();
  }, [loadReview, loadState]);

  // Poll while finalisation jobs run.
  const jobs = review?.jobs ?? review?.session.jobs ?? null;
  const running = review?.session.status === "finalising" || Object.values(jobs ?? {}).some((j) => j.status === "pending" || j.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      void loadReview();
      void refetchState();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [running, loadReview]);

  // Debounced draft autosave.
  useEffect(() => {
    if (!draft || !draftDirty.current) return;
    setSaveState("saving");
    const timer = window.setTimeout(async () => {
      try {
        await meetppApi.putReview(sessionId, draft);
        setSaveState("saved");
      } catch {
        setSaveState("error");
      }
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [draft, sessionId]);

  const editDraft = (fn: (d: ReviewDraft) => ReviewDraft) => {
    draftDirty.current = true;
    setDraft((d) => fn(d ?? DEFAULT_DRAFT));
  };

  const previewPdf = async () => {
    setErr(null);
    const w = window.open("", "_blank");
    try {
      const blob = await meetppApi.reportPdf(sessionId);
      const url = URL.createObjectURL(blob);
      if (w) w.location.href = url;
      else window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 120000);
    } catch (e) {
      w?.close();
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const retryJobs = async () => {
    setBusy("retry");
    setErr(null);
    try {
      await meetppApi.finalise(sessionId);
      await loadReview();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const publish = async () => {
    if (!draft) return;
    if (!window.confirm(t("meetpp.review.publishConfirm", { defaultValue: "Publish the report and send the e-mails now? Recorded audio is deleted after publishing." }))) return;
    setBusy("publish");
    setErr(null);
    try {
      await meetppApi.putReview(sessionId, draft);
      const res = await meetppApi.publish(sessionId);
      setPublished(res);
      await loadReview();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  if (loadError && !review) {
    return (
      <div className="mx-auto max-w-2xl p-6 text-slate-700">
        <div className="rounded border border-rose-300 bg-rose-50 p-4 text-rose-700">{loadError}</div>
        <button className="mt-4 inline-flex items-center gap-1 rounded bg-slate-700 px-3 py-1.5 text-white" onClick={() => void loadReview()}>
          <RefreshCw size={14} /> {t("meetpp.review.retry", { defaultValue: "Retry" })}
        </button>
      </div>
    );
  }
  if (!review) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-400">
        <Loader2 className="animate-spin" />
      </div>
    );
  }

  const session = review.session;
  const isPublished = session.status === "published" || !!published;
  const title = session.series_title || t("meetpp.review.meeting", { defaultValue: "Meeting" });
  const date = session.started_at ? new Date(session.started_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
  const pendingDecisions = (snap?.decisions ?? []).filter((d) => !d.previous && d.status !== "pending");
  const unconfirmedVotes = pendingDecisions.some((d) => d.vote && !d.vote.confirmed);
  const failedJobs = Object.values(jobs ?? {}).some((j) => j.status === "failed");

  const NAV: Array<{ key: Sec; label: string; count?: number; dot?: boolean }> = [
    { key: "report", label: t("meetpp.review.nav.report", { defaultValue: "Meeting report" }), dot: failedJobs },
    { key: "attendance", label: t("meetpp.review.nav.attendance", { defaultValue: "Attendance" }), count: snap?.attendees.length },
    { key: "agenda", label: t("meetpp.review.nav.agenda", { defaultValue: "Agenda" }), count: snap?.sections.filter((s) => s.kind === "agenda" && !s.parent_id).length },
    { key: "decisions", label: t("meetpp.review.nav.decisions", { defaultValue: "Decisions · votes" }), count: pendingDecisions.length, dot: unconfirmedVotes },
    { key: "actions", label: t("meetpp.review.nav.actions", { defaultValue: "Follow-up actions" }), count: snap?.actions.length },
    { key: "papers", label: t("meetpp.review.nav.papers", { defaultValue: "Papers filed" }), count: snap ? snap.documents.length + snap.attachments.length : undefined },
    { key: "minutes", label: t("meetpp.review.nav.minutes", { defaultValue: "Minutes" }) },
    { key: "transcript", label: t("meetpp.review.nav.transcript", { defaultValue: "Transcript" }) },
    { key: "next", label: t("meetpp.review.nav.next", { defaultValue: "Next meeting" }) },
    { key: "distribution", label: t("meetpp.review.nav.distribution", { defaultValue: "Distribution" }) },
  ];

  const pick = (k: Sec) => {
    if (wide && (k === "next" || k === "distribution")) {
      rightRef.current?.querySelector(`[data-card="${k}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    setSec(k);
  };

  const rightCards = draft && (
    <>
      <div data-card="next">
        <NextMeetingCard draft={draft} edit={editDraft} review={review} />
      </div>
      <div data-card="distribution">
        <DistributionCard draft={draft} edit={editDraft} saveState={saveState} busy={busy === "publish"} published={isPublished} onPublish={() => void publish()} />
      </div>
    </>
  );

  return (
    <div className="min-h-dvh bg-slate-100 text-slate-800">
      <header className="flex flex-wrap items-center gap-3 bg-slate-900 px-6 py-4 text-white">
        <h1 className="min-w-0 flex-1 truncate text-xl font-bold">
          {t("meetpp.review.title", { defaultValue: "Meet++ review" })} — {title}
          {date ? ` · ${date}` : ""}
        </h1>
        <Pill tone={isPublished ? "green" : session.status === "finalising" ? "amber" : "blue"}>
          {isPublished
            ? t("meetpp.review.statusPublished", { defaultValue: "Published" })
            : session.status === "finalising"
              ? t("meetpp.review.statusFinalising", { defaultValue: "Finalising…" })
              : t("meetpp.review.statusReview", { defaultValue: "Draft" })}
        </Pill>
        <button type="button" onClick={() => void previewPdf()} className="inline-flex items-center gap-1.5 rounded-full bg-amber-500 px-4 py-2 text-sm font-semibold text-white hover:bg-amber-600">
          {t("meetpp.review.previewPdf", { defaultValue: "Preview PDF" })} <ExternalLink size={14} />
        </button>
      </header>

      {(err || stateError) && (
        <div className="mx-6 mt-3 space-y-1">
          {err && <ErrorLine text={err} />}
          {stateError && (
            <ErrorLine text={t("meetpp.review.stateError", { defaultValue: "The live record could not be loaded ({{e}}). The report preview still works.", e: stateError })} />
          )}
        </div>
      )}

      <div className={cx("grid gap-4 p-4", wide ? "grid-cols-[230px_minmax(0,1fr)_380px]" : "grid-cols-1 md:grid-cols-[210px_minmax(0,1fr)]")}>
        <nav className="h-fit rounded-2xl bg-white p-2 shadow-sm" aria-label={t("meetpp.review.navLabel", { defaultValue: "Report sections" })}>
          {NAV.map((n) => (
            <button
              key={n.key}
              type="button"
              onClick={() => pick(n.key)}
              aria-current={sec === n.key ? "page" : undefined}
              className={cx("flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-[15px]", sec === n.key ? "bg-blue-100 font-semibold text-blue-800" : "text-slate-700 hover:bg-slate-50")}
            >
              <span className="min-w-0 flex-1 truncate">
                {n.label}
                {n.count !== undefined ? ` (${n.count})` : ""}
              </span>
              {n.dot && <span className="h-2.5 w-2.5 rounded-full bg-amber-500" />}
            </button>
          ))}
        </nav>

        <main className="min-w-0 rounded-2xl bg-white p-5 shadow-sm">
          {sec === "report" && <ReportSection review={review} jobs={jobs} busy={busy === "retry"} onRetry={() => void retryJobs()} published={published} sid={sessionId} />}
          {sec === "attendance" && <AttendanceSection sid={sessionId} />}
          {sec === "agenda" && <AgendaSection />}
          {sec === "decisions" && <DecisionsSection />}
          {sec === "actions" && <ActionsSection />}
          {sec === "papers" && <PapersSection />}
          {sec === "minutes" && <MinutesSection />}
          {sec === "transcript" && (
            <div className="h-[70vh]">
              <MeetppTranscript variant="review" className="rounded-xl border border-slate-200" />
            </div>
          )}
          {!wide && sec === "next" && rightCards && <div className="space-y-4">{rightCards}</div>}
          {!wide && sec === "distribution" && draft && (
            <DistributionCard draft={draft} edit={editDraft} saveState={saveState} busy={busy === "publish"} published={isPublished} onPublish={() => void publish()} />
          )}
        </main>

        {wide && (
          <aside ref={rightRef} className="space-y-4">
            {rightCards}
          </aside>
        )}
      </div>
      {!wide && sec !== "distribution" && draft && !isPublished && (
        <div className="sticky bottom-0 flex justify-end gap-2 border-t border-slate-200 bg-white/95 px-4 py-2 backdrop-blur">
          <span className="self-center text-xs text-slate-400">{saveLabel(saveState, t)}</span>
          <button type="button" onClick={() => setSec("distribution")} className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700">
            {t("meetpp.review.publish", { defaultValue: "Publish & send" })}
          </button>
        </div>
      )}
    </div>
  );
}

function saveLabel(s: "idle" | "saving" | "saved" | "error", t: (k: string, o: Record<string, unknown>) => string): string {
  if (s === "saving") return t("meetpp.review.saving", { defaultValue: "Saving draft…" });
  if (s === "saved") return t("meetpp.review.saved", { defaultValue: "Draft saved" });
  if (s === "error") return t("meetpp.review.saveFailed", { defaultValue: "Draft not saved" });
  return "";
}

function initialDraft(r: ReviewResponse, attendees: Array<{ name: string; email: string | null; required_next: boolean }>): ReviewDraft {
  if (r.review) {
    return {
      ...DEFAULT_DRAFT,
      ...r.review,
      next_meeting: { ...DEFAULT_DRAFT.next_meeting, ...(r.review.next_meeting ?? {}) },
      recipients: { ...DEFAULT_DRAFT.recipients, ...(r.review.recipients ?? {}) },
      distribution: { ...DEFAULT_DRAFT.distribution, ...(r.review.distribution ?? {}) },
      next_agenda: r.review.next_agenda ?? r.final?.next_agenda ?? [],
    };
  }
  const withEmail = attendees.filter((a) => a.email).map((a) => ({ name: a.name, email: a.email! }));
  return {
    ...DEFAULT_DRAFT,
    next_agenda: r.final?.next_agenda ?? [],
    recipients: {
      report: withEmail,
      invite: attendees.filter((a) => a.required_next && a.email).map((a) => ({ name: a.name, email: a.email! })),
    },
  };
}

function H2({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <h2 className="min-w-0 flex-1 text-2xl font-bold text-slate-900">{children}</h2>
      {right}
    </div>
  );
}

// ── Meeting report ────────────────────────────────────────────────────────

function useJobLabels() {
  const { t } = useTranslation();
  return {
    label: (k: string) =>
      ({
        tier2: t("meetpp.review.jobTier2", { defaultValue: "Refined transcript (Mac Studio)" }),
        compose: t("meetpp.review.jobCompose", { defaultValue: "Section minutes" }),
        final: t("meetpp.review.jobFinal", { defaultValue: "Opening, adjournment, record of voting, provenance" }),
        render: t("meetpp.review.jobRender", { defaultValue: "Report PDF" }),
      })[k] ?? k,
    status: (s: string) =>
      ({
        pending: t("meetpp.review.jobPending", { defaultValue: "waiting" }),
        running: t("meetpp.review.jobRunning", { defaultValue: "running" }),
        done: t("meetpp.review.jobDone", { defaultValue: "done" }),
        failed: t("meetpp.review.jobFailed", { defaultValue: "failed" }),
        skipped: t("meetpp.review.jobSkipped", { defaultValue: "skipped" }),
      })[s] ?? s,
  };
}

function useSectionStatusLabel() {
  const { t } = useTranslation();
  return (s: string) =>
    ({
      pending: t("meetpp.agenda.pending", { defaultValue: "upcoming" }),
      live: t("meetpp.agenda.live", { defaultValue: "live" }),
      done: t("meetpp.agenda.done", { defaultValue: "done" }),
      deferred: t("meetpp.agenda.deferred", { defaultValue: "deferred to next meeting" }),
      skipped: t("meetpp.agenda.skipped", { defaultValue: "skipped" }),
    })[s] ?? s;
}

function ReportSection({
  review,
  jobs,
  busy,
  onRetry,
  published,
  sid,
}: {
  review: ReviewResponse;
  jobs: Record<string, JobInfo> | null;
  busy: boolean;
  onRetry: () => void;
  published: { outputs: OutputDto[]; email_results: Array<Record<string, unknown>> } | null;
  sid: string;
}) {
  const { t } = useTranslation();
  const jobText = useJobLabels();
  const snap = useMeetpp((s) => s.snap);
  const summary = review.final?.summary;
  const lines = Array.isArray(summary) ? summary : summary ? [summary] : [];
  const verify = review.final?.verify ?? [];
  const jobList = Object.entries(jobs ?? {});
  const anyFailed = jobList.some(([, j]) => j.status === "failed");
  const session = review.session;
  const typeLabel =
    session.meeting_type === "board"
      ? t("meetpp.setup.board", { defaultValue: "Board meeting (formal)" })
      : session.meeting_type === "general_assembly"
        ? t("meetpp.setup.generalAssembly", { defaultValue: "General assembly (formal)" })
        : t("meetpp.setup.informal", { defaultValue: "Informal (no votes or quorum)" });
  return (
    <div className="space-y-5">
      <H2>{t("meetpp.review.nav.report", { defaultValue: "Meeting report" })}</H2>
      <div className="grid gap-2 text-sm sm:grid-cols-2">
        <Info label={t("meetpp.review.type", { defaultValue: "Meeting type" })} value={typeLabel} />
        <Info label={t("meetpp.review.held", { defaultValue: "Held" })} value={[fmtDateTime(session.started_at), fmtClock(session.ended_at, false)].filter(Boolean).join(" – ")} />
        {snap?.quorum && (
          <Info
            label={t("meetpp.review.quorum", { defaultValue: "Quorum" })}
            value={`${snap.quorum.voting_present}/${snap.quorum.voting_total} · ${t("meetpp.review.required", { defaultValue: "required" })} ${snap.quorum.required ?? "—"} · ${snap.quorum.met ? "✓" : "✕"}`}
          />
        )}
      </div>
      <div>
        <h3 className="mb-1 font-semibold text-slate-900">{t("meetpp.review.jobs", { defaultValue: "Preparation" })}</h3>
        {jobList.length === 0 ? (
          <p className="text-sm text-slate-500">{t("meetpp.review.noJobs", { defaultValue: "No finalisation jobs reported yet." })}</p>
        ) : (
          <ul className="space-y-1">
            {jobList.map(([k, j]) => (
              <li key={k} className="flex items-center gap-2 text-sm">
                <Pill tone={j.status === "done" ? "green" : j.status === "failed" ? "red" : j.status === "skipped" ? "slate" : "amber"}>
                  {j.status === "running" || j.status === "pending" ? <Loader2 size={11} className="animate-spin" /> : null}
                  {jobText.status(j.status)}
                </Pill>
                <span>{jobText.label(k)}</span>
                {j.error && <span className="text-xs text-rose-600">{String(j.error)}</span>}
              </li>
            ))}
          </ul>
        )}
        {anyFailed && (
          <button type="button" disabled={busy} onClick={onRetry} className="mt-2 inline-flex items-center gap-1 rounded-lg bg-slate-800 px-3 py-1.5 text-sm text-white hover:bg-slate-900 disabled:opacity-60">
            {busy ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} {t("meetpp.review.retryJobs", { defaultValue: "Retry failed jobs" })}
          </button>
        )}
      </div>
      {lines.length > 0 && (
        <div>
          <h3 className="mb-1 font-semibold text-slate-900">{t("meetpp.review.summary", { defaultValue: "Summary" })}</h3>
          <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
            {lines.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </div>
      )}
      {verify.length > 0 && (
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-3">
          <h3 className="mb-1 font-semibold text-amber-900">{t("meetpp.review.verify", { defaultValue: "Please verify" })}</h3>
          <ul className="list-disc space-y-0.5 pl-5 text-sm text-amber-900">
            {verify.map((v, i) => (
              <li key={i}>{v}</li>
            ))}
          </ul>
        </div>
      )}
      {published && <PublishedOutputs sid={sid} outputs={published.outputs} emails={published.email_results} />}
    </div>
  );
}

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2">
      <div className="text-[11px] uppercase text-slate-500">{label}</div>
      <div className="text-slate-800">{value || "—"}</div>
    </div>
  );
}

function PublishedOutputs({ sid, outputs, emails }: { sid: string; outputs: OutputDto[]; emails: Array<Record<string, unknown>> }) {
  const { t } = useTranslation();
  const download = async (o: OutputDto) => {
    const blob = await meetppApi.outputBlob(sid, o.id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = o.filename;
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 10000);
  };
  return (
    <div className="rounded-xl border border-emerald-300 bg-emerald-50 p-3">
      <h3 className="mb-1 font-semibold text-emerald-900">{t("meetpp.review.publishedTitle", { defaultValue: "Published" })}</h3>
      <div className="flex flex-wrap gap-2">
        {outputs.map((o) => (
          <button key={o.id} type="button" onClick={() => void download(o)} className="inline-flex items-center gap-1 rounded-lg border border-emerald-400 bg-white px-3 py-1.5 text-sm text-emerald-900 hover:bg-emerald-100">
            <Download size={13} /> {o.filename}
          </button>
        ))}
      </div>
      {emails.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-emerald-900">
          {emails.map((e, i) => (
            <li key={i}>{Object.values(e).map(String).join(" · ")}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Attendance / agenda / actions / papers ────────────────────────────────

function AttendanceSection({ sid }: { sid: string }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  if (!snap) return <Missing />;
  return (
    <div>
      <H2>{t("meetpp.review.nav.attendance", { defaultValue: "Attendance" })}</H2>
      <AttendanceTable
        attendees={snap.attendees}
        canEdit
        onPatch={async (id, patch) => {
          await meetppApi.patchAttendee(sid, id, patch as never);
          await refetchState();
        }}
      />
    </div>
  );
}

function AgendaSection() {
  const { t } = useTranslation();
  const statusLabel = useSectionStatusLabel();
  const snap = useMeetpp((s) => s.snap);
  if (!snap) return <Missing />;
  const rows = outlineOrder(snap.sections).filter((s) => s.status !== "skipped");
  return (
    <div>
      <H2>{t("meetpp.review.nav.agenda", { defaultValue: "Agenda" })}</H2>
      <ol className="space-y-2">
        {rows.map((s) => (
          <li key={s.id} className={cx("rounded-lg border border-slate-200 px-3 py-2", s.parent_id && "ml-6")}>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-slate-900">{sectionLabel(s)}</span>
              <span className="flex-1" />
              <Pill tone={s.status === "done" ? "green" : s.status === "deferred" ? "amber" : "slate"}>{statusLabel(s.status)}</Pill>
            </div>
            {s.body && <div className="mt-1 whitespace-pre-wrap text-sm text-slate-600">{s.body}</div>}
          </li>
        ))}
      </ol>
    </div>
  );
}

function ActionsSection() {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  if (!snap) return <Missing />;
  const previous = snap.actions.filter((a) => a.previous);
  const fresh = snap.actions.filter((a) => !a.previous);
  return (
    <div className="space-y-4">
      <H2>{t("meetpp.review.nav.actions", { defaultValue: "Follow-up actions" })}</H2>
      {previous.length > 0 && (
        <div className="space-y-2">
          <h3 className="font-semibold text-slate-700">{t("meetpp.review.previousActions", { defaultValue: "Reported at this meeting (previous actions)" })}</h3>
          {previous.map((a) => (
            <ActionCard key={a.id} a={a} canEdit />
          ))}
        </div>
      )}
      <div className="space-y-2">
        <h3 className="font-semibold text-slate-700">{t("meetpp.review.newActions", { defaultValue: "New actions" })}</h3>
        {fresh.length === 0 && <p className="text-sm text-slate-400">{t("meetpp.review.none", { defaultValue: "None." })}</p>}
        {fresh.map((a) => (
          <ActionCard key={a.id} a={a} canEdit />
        ))}
      </div>
    </div>
  );
}

function PapersSection() {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  if (!snap) return <Missing />;
  return (
    <div className="space-y-3">
      <H2>{t("meetpp.review.nav.papers", { defaultValue: "Papers filed" })}</H2>
      {snap.documents.map((d) => (
        <DocumentRow key={d.id} doc={d} />
      ))}
      <div className="flex flex-wrap gap-3">
        {snap.attachments.map((a) => (
          <AttachmentCard key={a.id} att={a} canEdit />
        ))}
      </div>
      {snap.documents.length + snap.attachments.length === 0 && <p className="text-sm text-slate-400">{t("meetpp.review.none", { defaultValue: "None." })}</p>}
    </div>
  );
}

function Missing() {
  const { t } = useTranslation();
  return <p className="text-sm text-slate-400">{t("meetpp.review.missing", { defaultValue: "The meeting record is not available." })}</p>;
}

// ── Decisions with the vote editor ────────────────────────────────────────

function DecisionsSection() {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  if (!snap) return <Missing />;
  const sections = new Map(snap.sections.map((s) => [s.id, s]));
  const list = snap.decisions.filter((d) => !d.previous);
  const taken = list.filter((d) => d.status !== "pending");
  const notTaken = list.filter((d) => d.status === "pending");
  return (
    <div className="space-y-4">
      <H2>{t("meetpp.review.nav.decisions", { defaultValue: "Decisions · votes" })}</H2>
      {taken.length === 0 && <p className="text-sm text-slate-400">{t("meetpp.review.noDecisions", { defaultValue: "No decisions were taken." })}</p>}
      {taken.map((d) => (
        <div key={d.id} className="space-y-2 rounded-2xl border border-slate-200 p-3">
          {d.section_id && sections.get(d.section_id) && <div className="text-xs font-semibold uppercase text-slate-500">{sectionLabel(sections.get(d.section_id) as SectionDto)}</div>}
          <DecisionCard d={d} canEdit />
          <VoteEditor d={d} formal={snap.session.meeting_type !== "informal"} />
        </div>
      ))}
      {notTaken.length > 0 && (
        <div className="rounded-xl border border-dashed border-slate-300 p-3">
          <h3 className="mb-1 font-semibold text-slate-600">{t("meetpp.review.notTaken", { defaultValue: "To decide, not taken (left out of the report)" })}</h3>
          <ul className="list-disc pl-5 text-sm text-slate-600">
            {notTaken.map((d) => (
              <li key={d.id}>
                {d.ref} {d.title}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── Minutes ───────────────────────────────────────────────────────────────

function MinutesSection() {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  if (!snap) return <Missing />;
  const order = outlineOrder(snap.sections);
  const parts = ["opening", "voting_record", "adjournment", "provenance"]
    .map((k) => snap.minutes.find((m) => m.kind === k))
    .filter((m): m is NonNullable<typeof m> => !!m);
  const partLabel: Record<string, string> = {
    opening: t("meetpp.minutes.opening", { defaultValue: "Opening" }),
    adjournment: t("meetpp.minutes.adjournment", { defaultValue: "Adjournment" }),
    voting_record: t("meetpp.minutes.votingRecord", { defaultValue: "Record of voting" }),
    provenance: t("meetpp.minutes.provenance", { defaultValue: "Provenance" }),
  };
  return (
    <div className="space-y-5">
      <H2>{t("meetpp.review.nav.minutes", { defaultValue: "Minutes" })}</H2>
      {parts
        .filter((m) => m.kind === "opening")
        .map((m) => (
          <MinutesPart key={m.id} title={partLabel[m.kind]}>
            <MinutesBlock m={m} section={null} isLive={false} canChair={false} canEdit />
          </MinutesPart>
        ))}
      {order.map((s) => {
        const m = snap.minutes.find((x) => x.section_id === s.id && (x.kind === "section" || !x.kind)) ?? null;
        if (!m && s.status !== "done") return null;
        return (
          <MinutesPart key={s.id} title={sectionLabel(s)} sub={!!s.parent_id}>
            <MinutesBlock m={m} section={s} isLive={false} canChair canEdit />
          </MinutesPart>
        );
      })}
      {parts
        .filter((m) => m.kind !== "opening")
        .map((m) => (
          <MinutesPart key={m.id} title={partLabel[m.kind] ?? m.kind}>
            <MinutesBlock m={m} section={null} isLive={false} canChair={false} canEdit />
          </MinutesPart>
        ))}
    </div>
  );
}

function MinutesPart({ title, children, sub }: { title: string; children: ReactNode; sub?: boolean }) {
  return (
    <section className={cx(sub && "ml-6")}>
      <h3 className="mb-1 text-lg font-bold text-blue-900">{title}</h3>
      {children}
    </section>
  );
}

// ── Next meeting & distribution ───────────────────────────────────────────

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-2xl bg-white p-5 shadow-sm">
      <h2 className="mb-3 text-xl font-bold text-slate-900">{title}</h2>
      {children}
    </section>
  );
}

function NextMeetingCard({ draft, edit, review }: { draft: ReviewDraft; edit: (fn: (d: ReviewDraft) => ReviewDraft) => void; review: ReviewResponse }) {
  const { t } = useTranslation();
  const attendees = useMeetpp((s) => s.snap?.attendees ?? []);
  const required = useMemo(() => {
    const names = new Map<string, { name: string; reason?: string | null; email?: string | null }>();
    for (const a of attendees) if (a.required_next) names.set(a.name, { name: a.name, reason: a.required_reason, email: a.email });
    for (const r of review.final?.required_next ?? []) if (!names.has(r.name)) names.set(r.name, r);
    return [...names.values()];
  }, [attendees, review.final]);
  const nm = draft.next_meeting;
  const localValue = nm.date_iso ? toLocalInput(nm.date_iso) : "";
  const input = "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800";
  return (
    <Card title={t("meetpp.review.nav.next", { defaultValue: "Next meeting" })}>
      <div className="grid grid-cols-[90px_1fr] items-center gap-2 text-sm">
        <span className="text-slate-500">{t("meetpp.review.date", { defaultValue: "Date" })}</span>
        <input
          type="datetime-local"
          className={input}
          value={localValue}
          onChange={(e) => edit((d) => ({ ...d, next_meeting: { ...d.next_meeting, date_iso: e.target.value ? new Date(e.target.value).toISOString() : null } }))}
        />
        <span className="text-slate-500">{t("meetpp.review.duration", { defaultValue: "Duration" })}</span>
        <select className={input} value={nm.duration_min ?? 60} onChange={(e) => edit((d) => ({ ...d, next_meeting: { ...d.next_meeting, duration_min: Number(e.target.value) } }))}>
          {[30, 45, 60, 90, 120, 180].map((m) => (
            <option key={m} value={m}>
              {t("meetpp.agenda.timebox", { defaultValue: "{{n}} min", n: m })}
            </option>
          ))}
        </select>
        <span className="text-slate-500">{t("meetpp.review.room", { defaultValue: "Room" })}</span>
        <select className={input} value={nm.room} onChange={(e) => edit((d) => ({ ...d, next_meeting: { ...d.next_meeting, room: e.target.value as "same" | "new" } }))}>
          <option value="same">{t("meetpp.review.roomSame", { defaultValue: "same room (series)" })}</option>
          <option value="new">{t("meetpp.review.roomNew", { defaultValue: "new room" })}</option>
        </select>
      </div>
      {required.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-1.5 font-semibold text-slate-900">{t("meetpp.review.requiredAttendees", { defaultValue: "Required attendees" })}</h3>
          <div className="flex flex-wrap gap-1.5">
            {required.map((r) => (
              <span key={r.name} title={r.reason ?? undefined} className="rounded-full bg-blue-100 px-3 py-1 text-sm text-blue-900">
                {r.name}
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="mt-4">
        <h3 className="mb-1.5 font-semibold text-slate-900">
          {t("meetpp.review.nextAgenda", { defaultValue: "Agenda draft ({{n}} points)", n: draft.next_agenda.length })}
        </h3>
        <ol className="space-y-1.5">
          {draft.next_agenda.map((p, i) => (
            <li key={i} className="flex items-start gap-1.5">
              <span className="mt-1.5 w-5 text-right text-sm text-slate-500">{i + 1}.</span>
              <div className="min-w-0 flex-1 space-y-1">
                <input
                  className="w-full rounded border border-slate-300 px-2 py-1 text-sm text-slate-800"
                  value={p.title}
                  onChange={(e) => edit((d) => ({ ...d, next_agenda: d.next_agenda.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)) }))}
                />
                {p.body !== undefined && p.body !== null && p.body !== "" && <div className="text-xs text-slate-500">{p.body}</div>}
              </div>
              <button type="button" className="mt-1 rounded p-1 text-rose-600 hover:bg-rose-50" onClick={() => edit((d) => ({ ...d, next_agenda: d.next_agenda.filter((_, j) => j !== i) }))} aria-label={t("meetpp.setup.deletePoint", { defaultValue: "Delete point" })}>
                <Trash2 size={13} />
              </button>
            </li>
          ))}
        </ol>
        <button type="button" className="mt-1.5 inline-flex items-center gap-1 text-sm font-medium text-blue-700 hover:underline" onClick={() => edit((d) => ({ ...d, next_agenda: [...d.next_agenda, { title: "" }] }))}>
          <Plus size={13} /> {t("meetpp.setup.addPoint", { defaultValue: "Add point" })}
        </button>
      </div>
      <RecipientList
        title={t("meetpp.review.inviteRecipients", { defaultValue: "Invitation recipients" })}
        list={draft.recipients.invite}
        onChange={(invite) => edit((d) => ({ ...d, recipients: { ...d.recipients, invite } }))}
      />
    </Card>
  );
}

function toLocalInput(iso: string): string {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function RecipientList({ title, list, onChange }: { title: string; list: Recipient[]; onChange: (l: Recipient[]) => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const add = () => {
    if (!email.includes("@")) return;
    onChange([...list, { name: name.trim() || email.trim(), email: email.trim() }]);
    setName("");
    setEmail("");
  };
  return (
    <div className="mt-4">
      <h3 className="mb-1.5 font-semibold text-slate-900">
        {title} ({list.length})
      </h3>
      <ul className="space-y-1 text-sm">
        {list.map((r, i) => (
          <li key={`${r.email}-${i}`} className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate">
              {r.name} <span className="text-slate-400">&lt;{r.email}&gt;</span>
            </span>
            <button type="button" className="rounded p-0.5 text-slate-400 hover:text-rose-600" onClick={() => onChange(list.filter((_, j) => j !== i))} aria-label={t("meetpp.review.removeRecipient", { defaultValue: "Remove" })}>
              <X size={13} />
            </button>
          </li>
        ))}
      </ul>
      <div className="mt-1.5 flex gap-1.5">
        <input className="w-1/3 min-w-0 rounded border border-slate-300 px-2 py-1 text-sm text-slate-800" placeholder={t("meetpp.review.name", { defaultValue: "Name" })} value={name} onChange={(e) => setName(e.target.value)} />
        <input
          className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-sm text-slate-800"
          placeholder={t("meetpp.review.email", { defaultValue: "E-mail" })}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button type="button" onClick={add} className="rounded border border-slate-300 px-2 text-sm text-slate-700 hover:bg-slate-50" aria-label={t("meetpp.review.addRecipient", { defaultValue: "Add recipient" })}>
          <Plus size={13} />
        </button>
      </div>
    </div>
  );
}

function DistributionCard({
  draft,
  edit,
  saveState,
  busy,
  published,
  onPublish,
}: {
  draft: ReviewDraft;
  edit: (fn: (d: ReviewDraft) => ReviewDraft) => void;
  saveState: "idle" | "saving" | "saved" | "error";
  busy: boolean;
  published: boolean;
  onPublish: () => void;
}) {
  const { t } = useTranslation();
  const dist = draft.distribution;
  const toggle = (k: keyof ReviewDraft["distribution"]) => edit((d) => ({ ...d, distribution: { ...d.distribution, [k]: !d.distribution[k] } }));
  return (
    <Card title={t("meetpp.review.nav.distribution", { defaultValue: "Distribution" })}>
      <div className="space-y-3">
        <Toggle on={dist.send_report} onClick={() => toggle("send_report")}>
          {t("meetpp.review.sendReport", { defaultValue: "Send meeting report PDF to members ({{n}})", n: draft.recipients.report.length })}
        </Toggle>
        <Toggle on={dist.send_invites} onClick={() => toggle("send_invites")}>
          {t("meetpp.review.sendInvites", { defaultValue: "Send invite + agenda (.ics) to required ({{n}})", n: draft.recipients.invite.length })}
        </Toggle>
        <Toggle on={dist.attach_snapshots} onClick={() => toggle("attach_snapshots")}>
          {t("meetpp.review.attachSnapshots", { defaultValue: "Attach whiteboard snapshots" })}
        </Toggle>
        <Toggle on={dist.include_transcript} onClick={() => toggle("include_transcript")}>
          {t("meetpp.review.includeTranscript", { defaultValue: "Include full transcript" })}
        </Toggle>
      </div>
      <RecipientList
        title={t("meetpp.review.reportRecipients", { defaultValue: "Report recipients" })}
        list={draft.recipients.report}
        onChange={(report) => edit((d) => ({ ...d, recipients: { ...d.recipients, report } }))}
      />
      <div className="mt-5 flex items-center gap-2">
        <span className="min-w-0 flex-1 text-xs text-slate-400">{saveLabel(saveState, t)}</span>
        <button
          type="button"
          disabled={busy || published}
          onClick={onPublish}
          className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-5 py-2.5 text-base font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          {published ? t("meetpp.review.statusPublished", { defaultValue: "Published" }) : t("meetpp.review.publish", { defaultValue: "Publish & send" })}
        </button>
      </div>
      <p className="mt-2 text-center text-xs italic text-slate-500">{t("meetpp.review.nothingSent", { defaultValue: "nothing is sent before this click" })}</p>
    </Card>
  );
}

function Toggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" role="switch" aria-checked={on} onClick={onClick} className="flex w-full items-center gap-3 text-left text-sm text-slate-800">
      <span className={cx("relative h-6 w-11 flex-shrink-0 rounded-full transition-colors", on ? "bg-emerald-600" : "bg-slate-300")}>
        <span className={cx("absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all", on ? "left-[22px]" : "left-0.5")} />
      </span>
      {children}
    </button>
  );
}
