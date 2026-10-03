import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Check, Loader2, RefreshCw, Send } from "lucide-react";
import { meetppApi } from "../lib/meetpp/api";

interface ReviewData {
  session: { id: string; status: string; language: string; template: string; version: number };
  final: {
    summary?: string[];
    next_agenda?: Array<{ title: string; timebox_minutes?: number | null }>;
    required_next?: Array<{ name: string; reason?: string }>;
    changes?: Array<{ kind: string; id: string; note: string }>;
  } | null;
  booking: { title?: string; iso?: string; duration_min?: number; room?: string };
  agenda: Array<{ id: string; position: number; title: string; presenter: string | null; status: string }>;
  minutes: Array<{ id: string; item_id: string | null; body_md: string }>;
  decisions: Array<{ id: string; ref: string; text: string; status: string }>;
  actions: Array<{ id: string; ref: string; title: string; owner: string | null; due: string | null; status: string }>;
  attendance: Array<{ id: string; name: string; email: string | null; presence: string; required_next: boolean }>;
  attachments: Array<{ id: string; url: string; caption: string | null; kind: string }>;
  error: string | null;
}

type Section = "summary" | "agenda" | "decisions" | "actions" | "attendance" | "minutes" | "attachments" | "transcript" | "next" | "distribution";

export default function MeetppReview() {
  const { sessionId = "" } = useParams();
  const { t } = useTranslation();
  const [data, setData] = useState<ReviewData | null>(null);
  const [section, setSection] = useState<Section>("minutes");
  const [busy, setBusy] = useState(false);
  const [published, setPublished] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sendMinutes, setSendMinutes] = useState(true);
  const [sendInvites, setSendInvites] = useState(true);
  const [attachSnaps, setAttachSnaps] = useState(true);
  const [fullTranscript, setFullTranscript] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = (await meetppApi.getReview(sessionId)) as unknown as ReviewData;
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load review");
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Auto-save draft (booking + edited minutes) every 5 s.
  useEffect(() => {
    if (!data) return;
    const timer = window.setInterval(() => {
      void meetppApi
        .putReview(sessionId, { booking: data.booking, minutes: data.minutes.map((m) => ({ id: m.id, item_id: m.item_id, body_md: m.body_md })) })
        .catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [data, sessionId]);

  if (error && !data) {
    return (
      <div className="mx-auto max-w-2xl p-6 text-slate-700">
        <div className="rounded border border-rose-300 bg-rose-50 p-4 text-rose-700">{error}</div>
        <button className="mt-4 rounded bg-slate-700 px-3 py-1.5 text-white" onClick={() => void load()}>
          <RefreshCw size={14} className="inline" /> {t("meetpp.review.retry", { defaultValue: "Retry" })}
        </button>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-400">
        <Loader2 className="animate-spin" />
      </div>
    );
  }

  const title = data.booking.title ?? data.session.id;
  const updatedMinute = (id: string, body: string) =>
    setData((d) => (d ? { ...d, minutes: d.minutes.map((m) => (m.id === id ? { ...m, body_md: body } : m)) } : d));

  const publish = async () => {
    setBusy(true);
    setError(null);
    try {
      await meetppApi.putReview(sessionId, {
        booking: data.booking,
        final: (data.final ?? undefined) as Record<string, unknown> | undefined,
        minutes: data.minutes.map((m) => ({ id: m.id, item_id: m.item_id, body_md: m.body_md })),
      });
      await meetppApi.publish(sessionId);
      setPublished(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Publish failed");
    } finally {
      setBusy(false);
    }
  };

  const itemTitle = (itemId: string | null) => {
    const it = data.agenda.find((a) => a.id === itemId);
    return it ? `${it.position}. ${it.title}${it.presenter ? ` — ${it.presenter}` : ""}` : t("meetpp.review.general", { defaultValue: "General" });
  };

  const NAV: Array<{ key: Section; label: string; count?: number; dot?: boolean }> = [
    { key: "summary", label: t("meetpp.review.sections.summary", { defaultValue: "Summary" }) },
    { key: "agenda", label: t("meetpp.review.sections.agenda", { defaultValue: "Agenda" }), count: data.agenda.length },
    { key: "decisions", label: t("meetpp.review.sections.decisions", { defaultValue: "Decisions" }), count: data.decisions.length, dot: !!data.final?.changes?.length },
    { key: "actions", label: t("meetpp.review.sections.actions", { defaultValue: "Actions" }), count: data.actions.length, dot: !!data.final?.changes?.length },
    { key: "attendance", label: t("meetpp.review.sections.attendance", { defaultValue: "Attendance" }), count: data.attendance.length },
    { key: "minutes", label: t("meetpp.review.sections.minutes", { defaultValue: "Minutes" }) },
    { key: "attachments", label: t("meetpp.review.sections.attachments", { defaultValue: "Attachments" }), count: data.attachments.length },
    { key: "transcript", label: t("meetpp.review.sections.transcript", { defaultValue: "Transcript" }) },
    { key: "next", label: t("meetpp.review.nextMeeting.title", { defaultValue: "Next meeting" }) },
    { key: "distribution", label: t("meetpp.review.sections.distribution", { defaultValue: "Distribution" }) },
  ];

  return (
    <div className="min-h-screen bg-slate-50">
      {/* Dark top bar (FDD Figure 5) */}
      <div className="flex items-center justify-between bg-[#0E1E33] px-5 py-3">
        <div className="text-lg font-semibold text-white">
          Meet++ review — {title} · {data.booking.iso ? new Date(data.booking.iso).toLocaleDateString() : ""}
        </div>
        {published || data.session.status === "published" ? (
          <span className="rounded-full bg-emerald-500 px-3 py-1 text-sm font-medium text-white">
            {t("meetpp.review.published", { defaultValue: "Published" })}
          </span>
        ) : (
          <span className="rounded-full bg-amber-400 px-3 py-1 text-sm font-medium text-slate-900">
            {t("meetpp.review.draft", { defaultValue: "Draft · finalised {{time}}", time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) })}
          </span>
        )}
      </div>

      <div className="mx-auto grid max-w-[1400px] grid-cols-12 gap-4 p-4">
        {/* Left nav */}
        <nav className="col-span-2 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-slate-200">
          {NAV.map((n) => (
            <button
              key={n.key}
              onClick={() => setSection(n.key)}
              className={[
                "mb-0.5 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm",
                section === n.key ? "bg-blue-50 font-semibold text-blue-700" : "text-slate-600 hover:bg-slate-50",
              ].join(" ")}
            >
              {n.label}
              {n.count != null && <span className="text-xs text-slate-400">({n.count})</span>}
              {n.dot && <span className="ml-auto h-2 w-2 rounded-full bg-amber-500" />}
            </button>
          ))}
        </nav>

        {/* Centre */}
        <main className="col-span-7 rounded-2xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
          {section === "minutes" && (
            <>
              <div className="mb-3 flex items-center justify-between">
                <h1 className="text-2xl font-bold text-slate-900">{t("meetpp.review.sections.minutes", { defaultValue: "Minutes" })}</h1>
                <div className="text-sm">
                  <button className="text-blue-600 hover:underline" onClick={() => void load()}>↻ {t("meetpp.review.regenerate", { defaultValue: "Regenerate" })}</button>
                  <span className="mx-1 text-slate-300">·</span>
                  <button className="text-blue-600 hover:underline">✎ {t("meetpp.review.edit", { defaultValue: "Edit" })}</button>
                </div>
              </div>
              <div className="space-y-5">
                {data.minutes.length === 0 && <p className="text-sm text-slate-400">{t("meetpp.review.noMinutes", { defaultValue: "No minutes yet." })}</p>}
                {data.minutes.map((m) => (
                  <div key={m.id}>
                    <div className="text-base font-semibold text-slate-800">{itemTitle(m.item_id)}</div>
                    <textarea
                      className="mt-1 w-full resize-y rounded-lg border border-slate-200 bg-slate-50 p-2 text-sm text-slate-700 focus:border-blue-400 focus:bg-white"
                      rows={3}
                      value={m.body_md}
                      onChange={(e) => updatedMinute(m.id, e.target.value)}
                    />
                  </div>
                ))}
              </div>
              {data.attachments.filter((a) => a.kind === "whiteboard").length > 0 && (
                <div className="mt-5 space-y-3">
                  {data.attachments.filter((a) => a.kind === "whiteboard").map((a) => (
                    <div key={a.id}>
                      <img src={a.url} alt={a.caption ?? ""} className="w-full max-w-sm rounded-lg ring-1 ring-slate-200" />
                      <div className="mt-1 text-xs text-slate-400">{a.caption}</div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {section === "summary" && (
            <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
              {(data.final?.summary ?? []).map((b, i) => <li key={i}>{b}</li>)}
            </ul>
          )}
          {section === "agenda" && (
            <ul className="space-y-1 text-sm text-slate-700">
              {data.agenda.map((a) => <li key={a.id}>{a.position}. {a.title}{a.presenter ? ` — ${a.presenter}` : ""}</li>)}
            </ul>
          )}
          {section === "decisions" && (
            <ul className="space-y-1 text-sm text-slate-700">
              {data.decisions.map((d) => <li key={d.id}><span className="mr-1 font-semibold text-blue-700">{d.ref}</span>{d.text} <span className="text-xs text-slate-400">({d.status})</span></li>)}
            </ul>
          )}
          {section === "actions" && (
            <ul className="space-y-1 text-sm text-slate-700">
              {data.actions.map((a) => <li key={a.id}><span className="mr-1 font-semibold text-blue-700">{a.ref}</span>{a.title} — {a.owner ?? "—"} · {a.due ?? "—"} <span className="text-xs text-slate-400">({a.status})</span></li>)}
            </ul>
          )}
          {section === "attendance" && (
            <ul className="space-y-1 text-sm text-slate-700">
              {data.attendance.map((a) => <li key={a.id}>{a.name} — {a.presence}{a.required_next ? " · required next" : ""}</li>)}
            </ul>
          )}
          {section === "attachments" && (
            <ul className="space-y-1 text-sm text-slate-700">
              {data.attachments.map((a) => <li key={a.id}><a className="text-blue-600 hover:underline" href={a.url} target="_blank" rel="noreferrer">{a.caption ?? a.kind}</a></li>)}
            </ul>
          )}
          {section === "transcript" && (
            <p className="text-sm text-slate-400">{t("meetpp.review.transcriptHint", { defaultValue: "The full transcript is included in the session export." })}</p>
          )}
          {section === "next" && <NextMeeting data={data} setData={setData} />}
          {section === "distribution" && (
            <Distribution
              sendMinutes={sendMinutes} setSendMinutes={setSendMinutes}
              sendInvites={sendInvites} setSendInvites={setSendInvites}
              attachSnaps={attachSnaps} setAttachSnaps={setAttachSnaps}
              fullTranscript={fullTranscript} setFullTranscript={setFullTranscript}
            />
          )}
        </main>

        {/* Right column */}
        <aside className="col-span-3 space-y-4">
          <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
            <NextMeeting data={data} setData={setData} compact />
          </div>
          <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
            <Distribution
              sendMinutes={sendMinutes} setSendMinutes={setSendMinutes}
              sendInvites={sendInvites} setSendInvites={setSendInvites}
              attachSnaps={attachSnaps} setAttachSnaps={setAttachSnaps}
              fullTranscript={fullTranscript} setFullTranscript={setFullTranscript}
            />
            <div className="mt-4 flex items-center justify-end gap-2">
              <button className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700">
                {t("meetpp.review.saveDraft", { defaultValue: "Save draft" })}
              </button>
              <button
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
                disabled={busy || published}
                onClick={() => void publish()}
              >
                {busy ? <Loader2 size={14} className="animate-spin" /> : published ? <Check size={14} /> : <Send size={14} />}
                {published ? t("meetpp.review.published", { defaultValue: "Published" }) : t("meetpp.review.publish", { defaultValue: "Publish & send" })}
              </button>
            </div>
            <div className="mt-2 text-right text-xs italic text-slate-400">
              {t("meetpp.review.nothingSent", { defaultValue: "nothing is sent before this click" })}
            </div>
          </div>
          {data.error && <div className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700">{data.error}</div>}
          {error && <div className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</div>}
        </aside>
      </div>
    </div>
  );
}

function NextMeeting({ data, setData, compact }: { data: ReviewData; setData: (d: ReviewData) => void; compact?: boolean }) {
  const { t } = useTranslation();
  return (
    <div>
      <h2 className={compact ? "mb-2 text-base font-bold text-slate-900" : "mb-3 text-2xl font-bold text-slate-900"}>
        {t("meetpp.review.nextMeeting.title", { defaultValue: "Next meeting" })}
      </h2>
      <div className="space-y-2 text-sm">
        <label className="flex items-center justify-between gap-2">
          <span className="text-slate-500">{t("meetpp.review.nextMeeting.date", { defaultValue: "Date" })}</span>
          <input
            type="datetime-local"
            className="rounded-lg border border-slate-300 px-2 py-1 text-sm"
            value={(data.booking.iso ?? "").slice(0, 16)}
            onChange={(e) => setData({ ...data, booking: { ...data.booking, iso: e.target.value } })}
          />
        </label>
        <label className="flex items-center justify-between gap-2">
          <span className="text-slate-500">{t("meetpp.review.nextMeeting.duration", { defaultValue: "Duration" })}</span>
          <input
            type="number"
            className="w-24 rounded-lg border border-slate-300 px-2 py-1 text-sm"
            value={data.booking.duration_min ?? 60}
            onChange={(e) => setData({ ...data, booking: { ...data.booking, duration_min: Number(e.target.value) } })}
          />
        </label>
      </div>
      {data.final?.required_next && data.final.required_next.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-xs font-semibold text-slate-600">{t("meetpp.review.nextMeeting.required", { defaultValue: "Required attendees" })}</div>
          <div className="flex flex-wrap gap-1.5">
            {data.final.required_next.map((r, i) => (
              <span key={i} title={r.reason} className="rounded-full bg-blue-100 px-2.5 py-0.5 text-xs text-blue-700">{r.name}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="mb-2 flex cursor-pointer items-center gap-2 text-sm text-slate-700">
      <button
        type="button"
        onClick={() => onChange(!on)}
        className={["relative h-5 w-9 rounded-full transition", on ? "bg-emerald-500" : "bg-slate-300"].join(" ")}
      >
        <span className={["absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all", on ? "left-4" : "left-0.5"].join(" ")} />
      </button>
      {label}
    </label>
  );
}

function Distribution(props: {
  sendMinutes: boolean; setSendMinutes: (v: boolean) => void;
  sendInvites: boolean; setSendInvites: (v: boolean) => void;
  attachSnaps: boolean; setAttachSnaps: (v: boolean) => void;
  fullTranscript: boolean; setFullTranscript: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <div>
      <h2 className="mb-2 text-base font-bold text-slate-900">{t("meetpp.review.sections.distribution", { defaultValue: "Distribution" })}</h2>
      <Toggle on={props.sendMinutes} onChange={props.setSendMinutes} label={t("meetpp.review.distribution.minutesToAttendees", { defaultValue: "Send minutes PDF to attendees" })} />
      <Toggle on={props.sendInvites} onChange={props.setSendInvites} label={t("meetpp.review.distribution.invites", { defaultValue: "Send invite + agenda (.ics) to required" })} />
      <Toggle on={props.attachSnaps} onChange={props.setAttachSnaps} label={t("meetpp.review.distribution.attachSnaps", { defaultValue: "Attach whiteboard snapshots" })} />
      <Toggle on={props.fullTranscript} onChange={props.setFullTranscript} label={t("meetpp.review.distribution.includeTranscript", { defaultValue: "Include full transcript" })} />
    </div>
  );
}
