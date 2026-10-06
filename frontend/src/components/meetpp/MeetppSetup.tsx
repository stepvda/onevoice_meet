import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, FileUp, GripVertical, Loader2, Pencil, Plus, Trash2, X } from "lucide-react";
import { meetppApi, type OutlinePointInput } from "../../lib/meetpp/api";
import { discover, loadSession } from "../../lib/meetpp/session";
import { applySnapshot, useMeetpp } from "../../lib/meetpp/store";
import { sortSections } from "../../lib/meetpp/state";
import type { DocSummary, DocumentDto, MajorityRule, MeetingType, SectionDto, SeriesDto, Snapshot } from "../../lib/meetpp/types";
import MeetppBoard from "./MeetppBoard";
import { cx } from "./ui";

/**
 * "Start Meet++" (FDD §5.12, Figure 5): template, English (fixed), AI mode
 * (Lead default), meeting type with the members / quorum / majority row,
 * agenda + previous-notes PDF uploads with parse polling, the agenda editor
 * bound to PUT /outline (usable with or without a PDF), a read-only preview
 * of the prefilled board (contract §1a), the consent notice, and Start.
 */

interface Props {
  meetingId: string;
  roomName: string;
  onClose: () => void;
  onStarted: (sid: string) => void;
}

interface EditorSub {
  key: string;
  id?: string;
  title: string;
  body: string;
}

interface EditorPoint {
  key: string;
  id?: string;
  title: string;
  body: string;
  presenter: string;
  timebox: string;
  subpoints: EditorSub[];
  open: boolean;
}

interface UploadState {
  name: string;
  status: "uploading" | "parsing" | "done" | "failed";
  doc?: DocumentDto;
  summary?: DocSummary | null;
  error?: string | null;
}

let keySeq = 0;
const newKey = () => `k${++keySeq}`;
const MAX_PDF = 10 * 1024 * 1024;

function pointsFromSections(sections: SectionDto[]): EditorPoint[] {
  const sorted = sortSections(sections);
  return sorted
    .filter((s) => s.kind === "agenda" && !s.parent_id)
    .map((p) => ({
      key: newKey(),
      id: p.id,
      title: p.title,
      body: p.body ?? "",
      presenter: p.presenter ?? "",
      timebox: p.timebox_minutes ? String(p.timebox_minutes) : "",
      open: false,
      subpoints: sorted.filter((c) => c.parent_id === p.id).map((c) => ({ key: newKey(), id: c.id, title: c.title, body: c.body ?? "" })),
    }));
}

function filled(points: EditorPoint[]): EditorPoint[] {
  return points.filter((p) => p.title.trim());
}

function toOutline(points: EditorPoint[]): OutlinePointInput[] {
  return filled(points).map((p) => ({
    ...(p.id ? { id: p.id } : {}),
    title: p.title.trim(),
    body: p.body.trim() || null,
    presenter: p.presenter.trim() || null,
    timebox_minutes: p.timebox ? Math.max(1, Math.round(Number(p.timebox))) || null : null,
    subpoints: p.subpoints.filter((s) => s.title.trim()).map((s) => ({ ...(s.id ? { id: s.id } : {}), title: s.title.trim(), body: s.body.trim() || null })),
  }));
}

export default function MeetppSetup({ meetingId, roomName, onClose, onStarted }: Props) {
  const { t } = useTranslation();
  const providerLabel = useMeetpp((s) => s.providerLabel);
  const [sid, setSid] = useState<string | null>(null);
  const [series, setSeries] = useState<SeriesDto | null>(null);
  const [template, setTemplate] = useState<"agenda" | "goal">("agenda");
  const [mode, setMode] = useState<"lead" | "assist">("lead");
  const [meetingType, setMeetingType] = useState<MeetingType>("informal");
  const [majority, setMajority] = useState<MajorityRule>("ordinary");
  const [quorum, setQuorum] = useState<number | null>(null);
  const [rosterCount, setRosterCount] = useState<number | null>(null);
  const [editRules, setEditRules] = useState(false);
  const [goal, setGoal] = useState("");
  const [points, setPoints] = useState<EditorPoint[]>([]);
  const [uploads, setUploads] = useState<{ agenda?: UploadState; previous_notes?: UploadState }>({});
  const [importedActions, setImportedActions] = useState(0);
  const [reviewSid, setReviewSid] = useState<string | null>(null);
  const [view, setView] = useState<"setup" | "preview">("setup");
  const [save, setSave] = useState<"idle" | "pending" | "saving" | "saved" | "error">("idle");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  const createdHere = useRef(false);
  const pointsRef = useRef(points);
  pointsRef.current = points;
  const sidRef = useRef(sid);
  sidRef.current = sid;
  const editRev = useRef(0);
  const saveTimer = useRef<number | null>(null);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, []);

  const fail = (e: unknown) => alive.current && setError(e instanceof Error ? e.message : String(e));

  const refreshRoster = useCallback(async (seriesId: string | null | undefined) => {
    if (!seriesId) return;
    try {
      const r = await meetppApi.getRoster(seriesId);
      if (alive.current) setRosterCount((r.roster ?? []).filter((m) => m.active !== false).length);
    } catch {
      /* optional */
    }
  }, []);

  /** Reload the setup snapshot (board preview + optional editor refresh). */
  const refreshState = useCallback(async (replaceAgenda: boolean): Promise<Snapshot | null> => {
    const id = sidRef.current;
    if (!id) return null;
    const snap = await meetppApi.getState(id);
    if (!alive.current) return null;
    applySnapshot(snap);
    if (replaceAgenda) setPoints(pointsFromSections(snap.sections));
    return snap;
  }, []);

  const adopt = useCallback(
    async (id: string) => {
      setSid(id);
      sidRef.current = id;
      const snap = await meetppApi.getState(id);
      if (!alive.current) return;
      applySnapshot(snap);
      const s = snap.session;
      setTemplate(s.template);
      setMode(s.mode ?? "lead");
      setGoal(s.goal ?? "");
      setMeetingType(s.meeting_type ?? "informal");
      setMajority(s.majority_rule ?? "ordinary");
      setPoints(pointsFromSections(snap.sections));
      setImportedActions(snap.actions.filter((a) => a.previous).length);
      const ups: { agenda?: UploadState; previous_notes?: UploadState } = {};
      for (const d of snap.documents) ups[d.kind] = { name: d.filename, status: d.status === "done" ? "done" : d.status === "failed" ? "failed" : "parsing", doc: d, summary: d.summary ?? null, error: d.error };
      setUploads(ups);
      setSeries({ id: s.series_id, title: s.series_title, meeting_type: s.meeting_type, majority_rule: s.majority_rule, quorum_required: null });
      void refreshRoster(s.series_id);
    },
    [refreshRoster],
  );

  const create = useCallback(
    async (tpl: "agenda" | "goal", keepPoints: boolean) => {
      const res = await meetppApi.createSession(meetingId, { template: tpl, mode, meeting_type: meetingType, ...(goal.trim() ? { goal: goal.trim() } : {}) });
      if (!alive.current) return;
      createdHere.current = true;
      setSid(res.session.id);
      sidRef.current = res.session.id;
      setSeries(res.series);
      if (res.series) {
        setQuorum(res.series.quorum_required ?? null);
        setMajority(res.series.majority_rule ?? "ordinary");
        if (res.series.meeting_type) setMeetingType(res.series.meeting_type);
      }
      setImportedActions(res.imported_actions?.length ?? 0);
      void refreshRoster(res.series?.id ?? res.session.series_id);
      const snap = await refreshState(!keepPoints);
      if (keepPoints && tpl === "agenda" && snap && pointsRef.current.length) {
        // Re-send the typed agenda to the new session (new ids).
        setPoints((p) => p.map((x) => ({ ...x, id: undefined, subpoints: x.subpoints.map((s) => ({ ...s, id: undefined })) })));
        scheduleSave();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [meetingId, mode, meetingType, goal, refreshRoster, refreshState],
  );

  // Reuse a session still in setup, else create one (fixed sections exist
  // immediately so uploads and the editor have a session to write to).
  const initStarted = useRef(false);
  useEffect(() => {
    // Once per modal (StrictMode re-runs effects; never create two sessions).
    if (initStarted.current) return;
    initStarted.current = true;
    void (async () => {
      try {
        const list = await meetppApi.listSessions(meetingId);
        const sessions = list.sessions ?? [];
        const rev = sessions.filter((s) => s.status === "review" || s.status === "finalising").pop();
        if (rev) setReviewSid(rev.id);
        const setup = sessions.filter((s) => s.status === "setup").pop();
        if (setup) await adopt(setup.id);
        else await create("agenda", false);
      } catch (e) {
        fail(e);
      } finally {
        if (alive.current) setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── outline saving (debounced PUT /outline) ──
  const flushSave = useCallback(async () => {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    const id = sidRef.current;
    if (!id) return;
    const rev = editRev.current;
    setSave("saving");
    try {
      const res = await meetppApi.putOutline(id, toOutline(pointsRef.current));
      if (!alive.current) return;
      if (rev === editRev.current) {
        const sorted = sortSections(res.sections ?? []);
        const tops = sorted.filter((s) => s.kind === "agenda" && !s.parent_id);
        const keys = filled(pointsRef.current);
        setPoints((cur) =>
          cur.map((p) => {
            const i = keys.findIndex((k) => k.key === p.key);
            const server = i >= 0 ? tops[i] : undefined;
            if (!server) return p;
            const subs = sorted.filter((c) => c.parent_id === server.id);
            const filledSubs = p.subpoints.filter((s) => s.title.trim());
            return {
              ...p,
              id: server.id,
              subpoints: p.subpoints.map((s) => {
                const j = filledSubs.findIndex((x) => x.key === s.key);
                return j >= 0 && subs[j] ? { ...s, id: subs[j].id } : s;
              }),
            };
          }),
        );
      }
      setSave("saved");
    } catch (e) {
      setSave("error");
      fail(e);
    }
  }, []);

  const scheduleSave = useCallback(() => {
    editRev.current++;
    setSave("pending");
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => void flushSave(), 800);
  }, [flushSave]);

  const updatePoints = (fn: (p: EditorPoint[]) => EditorPoint[]) => {
    setPoints(fn);
    scheduleSave();
  };

  // ── settings ──
  const switchTemplate = async (tpl: "agenda" | "goal") => {
    if (tpl === template || busy) return;
    setTemplate(tpl);
    setBusy(true);
    setError(null);
    try {
      const old = sidRef.current;
      if (old && createdHere.current) await meetppApi.deleteSession(old).catch(() => undefined);
      await create(tpl, true);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const changeMode = (m: "lead" | "assist") => {
    setMode(m);
    if (sid) void meetppApi.patchSession(sid, { mode: m }).catch(fail);
  };

  const changeType = (mt: MeetingType) => {
    setMeetingType(mt);
    if (series?.id) void meetppApi.putSeriesRules(series.id, { meeting_type: mt }).then((s) => alive.current && setSeries(s)).catch(fail);
  };

  const saveRules = async () => {
    if (!series?.id) return;
    try {
      const s = await meetppApi.putSeriesRules(series.id, { meeting_type: meetingType, majority_rule: majority, quorum_required: quorum });
      if (alive.current) {
        setSeries(s);
        setEditRules(false);
      }
    } catch (e) {
      fail(e);
    }
  };

  // ── uploads ──
  const upload = async (kind: "agenda" | "previous_notes", file: File) => {
    setError(null);
    if (!(file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"))) {
      setUploads((u) => ({ ...u, [kind]: { name: file.name, status: "failed", error: t("meetpp.setup.notPdf", { defaultValue: "Please choose a PDF file." }) } }));
      return;
    }
    if (file.size > MAX_PDF) {
      setUploads((u) => ({ ...u, [kind]: { name: file.name, status: "failed", error: t("meetpp.setup.tooLarge", { defaultValue: "The PDF is larger than 10 MB." }) } }));
      return;
    }
    const id = sidRef.current;
    if (!id) return;
    if (kind === "agenda") await flushSave();
    setUploads((u) => ({ ...u, [kind]: { name: file.name, status: "uploading" } }));
    try {
      const { document } = await meetppApi.uploadDocument(id, kind, file);
      await poll(id, kind, document);
    } catch (e) {
      if (alive.current) setUploads((u) => ({ ...u, [kind]: { name: file.name, status: "failed", error: e instanceof Error ? e.message : String(e) } }));
    }
  };

  const poll = async (id: string, kind: "agenda" | "previous_notes", doc: DocumentDto) => {
    let current = doc;
    for (let i = 0; i < 160 && alive.current; i++) {
      if (current.status === "done" || current.status === "failed") break;
      setUploads((u) => ({ ...u, [kind]: { name: current.filename, status: "parsing", doc: current } }));
      await new Promise((r) => setTimeout(r, 1500));
      const res = await meetppApi.getDocument(id, current.id);
      current = { ...res.document, summary: res.summary ?? res.document.summary ?? null };
    }
    if (!alive.current) return;
    const ok = current.status === "done";
    setUploads((u) => ({
      ...u,
      [kind]: { name: current.filename, status: ok ? "done" : "failed", doc: current, summary: current.summary ?? null, error: current.error },
    }));
    if (ok) {
      const snap = await refreshState(kind === "agenda");
      if (kind === "previous_notes" && snap) {
        setImportedActions(snap.actions.filter((a) => a.previous).length);
        void refreshRoster(series?.id ?? snap.session.series_id);
      }
    }
  };

  // ── start ──
  const start = async () => {
    const id = sidRef.current;
    if (!id) return;
    setBusy(true);
    setError(null);
    try {
      if (template === "agenda") await flushSave();
      if (template === "goal" && goal.trim()) await meetppApi.patchSession(id, { goal: goal.trim() });
      await meetppApi.start(id);
      const found = await discover(roomName);
      if (!found) await loadSession(id);
      onStarted(id);
    } catch (e) {
      fail(e);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const cancel = () => {
    const id = sidRef.current;
    const untouched = !filled(points).length && !uploads.agenda && !uploads.previous_notes;
    if (id && createdHere.current && untouched) void meetppApi.deleteSession(id).catch(() => undefined);
    onClose();
  };

  const formal = meetingType !== "informal";
  const provider = providerLabel || t("meetpp.setup.providerDefault", { defaultValue: "the configured AI provider" });
  const majorityLabel = (r: MajorityRule) =>
    ({
      ordinary: t("meetpp.rules.ordinary", { defaultValue: "simple majority" }),
      unanimous: t("meetpp.rules.unanimous", { defaultValue: "unanimity" }),
      two_thirds: t("meetpp.rules.twoThirds", { defaultValue: "two-thirds majority" }),
      four_fifths: t("meetpp.rules.fourFifths", { defaultValue: "four-fifths majority" }),
    })[r];
  const field = "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/70 p-3" role="dialog" aria-modal="true" aria-labelledby="meetpp-setup-title">
      <div className="flex max-h-[94vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl bg-white text-slate-800 shadow-2xl">
        <div className="flex items-start gap-3 border-b border-slate-200 px-6 pb-3 pt-5">
          <div className="min-w-0 flex-1">
            <h2 id="meetpp-setup-title" className="text-2xl font-bold text-slate-900">
              {t("meetpp.setup.title", { defaultValue: "Start Meet++" })}
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              {t("meetpp.setup.intro", { defaultValue: "Meet++ will transcribe the meeting, keep the agenda, decisions, actions and minutes up to date, and prepare the follow-up." })}
            </p>
          </div>
          <button type="button" onClick={cancel} className="rounded p-1 text-slate-500 hover:bg-slate-100" aria-label={t("meetpp.common.close", { defaultValue: "Close" })}>
            <X size={18} />
          </button>
        </div>
        <div className="flex gap-1 border-b border-slate-200 px-6" role="tablist">
          {(["setup", "preview"] as const).map((v) => (
            <button
              key={v}
              role="tab"
              aria-selected={view === v}
              onClick={() => {
                setView(v);
                if (v === "preview") void refreshState(false).catch(fail);
              }}
              className={cx("border-b-2 px-3 py-2 text-sm font-medium", view === v ? "border-blue-600 text-blue-700" : "border-transparent text-slate-500 hover:text-slate-800")}
            >
              {v === "setup" ? t("meetpp.setup.tabSetup", { defaultValue: "Setup" }) : t("meetpp.setup.tabPreview", { defaultValue: "Preview board" })}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {loading ? (
            <div className="flex h-40 items-center justify-center text-slate-400">
              <Loader2 className="animate-spin" />
            </div>
          ) : view === "preview" ? (
            <div className="h-[60vh] min-h-[420px]">
              <p className="mb-2 text-xs text-slate-500">
                {t("meetpp.setup.previewHint", { defaultValue: "What everyone will see when the meeting starts: the items to cover, prefilled from your agenda and previous notes." })}
              </p>
              <MeetppBoard variant="preview" className="h-[calc(100%-1.75rem)]" />
            </div>
          ) : (
            <div className="space-y-5">
              {reviewSid && (
                <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-900">
                  {t("meetpp.setup.reviewPending", { defaultValue: "The previous Meet++ session of this meeting is waiting for review." })}{" "}
                  <a className="font-semibold underline" href={`/meetings/${meetingId}/ai/${reviewSid}`} target="_blank" rel="noreferrer">
                    {t("meetpp.setup.openReview", { defaultValue: "Open review" })}
                  </a>
                </div>
              )}

              <Section n={1} title={t("meetpp.setup.structure", { defaultValue: "Meeting structure" })}>
                <div className="grid gap-3 sm:grid-cols-2">
                  {(["agenda", "goal"] as const).map((tpl) => (
                    <button
                      key={tpl}
                      type="button"
                      disabled={busy}
                      onClick={() => void switchTemplate(tpl)}
                      className={cx("flex items-start gap-3 rounded-xl border-2 p-3 text-left", template === tpl ? "border-blue-600 bg-blue-50" : "border-slate-200 hover:border-slate-300")}
                      aria-pressed={template === tpl}
                    >
                      <span className={cx("mt-1 grid h-5 w-5 flex-shrink-0 place-items-center rounded-full border-2", template === tpl ? "border-blue-600" : "border-slate-300")}>
                        {template === tpl && <span className="h-2.5 w-2.5 rounded-full bg-blue-600" />}
                      </span>
                      <span>
                        <span className="block font-semibold text-slate-900">
                          {tpl === "agenda" ? t("meetpp.setup.agendaDriven", { defaultValue: "Agenda-driven" }) : t("meetpp.setup.goalDriven", { defaultValue: "Goal-driven" })}
                        </span>
                        <span className="block text-xs text-slate-500">
                          {tpl === "agenda"
                            ? t("meetpp.setup.agendaDrivenHint", { defaultValue: "review actions → agenda → AOB → new actions" })
                            : t("meetpp.setup.goalDrivenHint", { defaultValue: "goal → in-meeting deliverables → next steps → planning" })}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
                {template === "goal" && (
                  <input
                    className={field}
                    value={goal}
                    placeholder={t("meetpp.setup.goalPlaceholder", { defaultValue: "The goal of this meeting" })}
                    onChange={(e) => setGoal(e.target.value)}
                    onBlur={() => sid && goal.trim() && void meetppApi.patchSession(sid, { goal: goal.trim() }).catch(fail)}
                  />
                )}
              </Section>

              <div className="grid gap-3 sm:grid-cols-3">
                <label className="block">
                  <span className="text-sm font-semibold text-slate-800">{t("meetpp.setup.language", { defaultValue: "Spoken language" })}</span>
                  <select className={field} value="en" disabled>
                    <option value="en">{t("meetpp.setup.english", { defaultValue: "English (en) — R1.1" })}</option>
                  </select>
                </label>
                <label className="block">
                  <span className="text-sm font-semibold text-slate-800">{t("meetpp.setup.mode", { defaultValue: "AI mode" })}</span>
                  <select className={field} value={mode} onChange={(e) => changeMode(e.target.value as "lead" | "assist")}>
                    <option value="lead">{t("meetpp.setup.modeLead", { defaultValue: "Lead — AI moves, chair can undo" })}</option>
                    <option value="assist">{t("meetpp.setup.modeAssist", { defaultValue: "Assist — AI proposes, chair moves" })}</option>
                  </select>
                </label>
                <label className="block">
                  <span className="text-sm font-semibold text-slate-800">{t("meetpp.setup.meetingType", { defaultValue: "Meeting type" })}</span>
                  <select className={field} value={meetingType} onChange={(e) => changeType(e.target.value as MeetingType)}>
                    <option value="informal">{t("meetpp.setup.informal", { defaultValue: "Informal (no votes or quorum)" })}</option>
                    <option value="board">{t("meetpp.setup.board", { defaultValue: "Board meeting (formal)" })}</option>
                    <option value="general_assembly">{t("meetpp.setup.generalAssembly", { defaultValue: "General assembly (formal)" })}</option>
                  </select>
                </label>
              </div>

              {formal && (
                <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-700">
                  {!editRules ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="min-w-0 flex-1">
                        {t("meetpp.setup.membersRow", {
                          defaultValue: "Members: taken from meeting participants · {{n}} known in this series · quorum {{q}} · {{rule}}",
                          n: rosterCount ?? "—",
                          q: quorum ?? series?.quorum_required ?? t("meetpp.rules.quorumDefault", { defaultValue: "majority of voting members" }),
                          rule: majorityLabel(majority),
                        })}
                      </span>
                      <button type="button" onClick={() => setEditRules(true)} className="inline-flex items-center gap-1 font-semibold text-blue-700 hover:underline">
                        {t("meetpp.setup.editRules", { defaultValue: "Edit rules" })} <Pencil size={12} />
                      </button>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-end gap-3">
                      <label className="block">
                        <span className="text-xs text-slate-500">{t("meetpp.rules.quorum", { defaultValue: "Quorum (members; empty = majority of voting members)" })}</span>
                        <input type="number" min={1} className={field} value={quorum ?? ""} onChange={(e) => setQuorum(e.target.value ? Number(e.target.value) : null)} />
                      </label>
                      <label className="block">
                        <span className="text-xs text-slate-500">{t("meetpp.rules.majority", { defaultValue: "Majority rule" })}</span>
                        <select className={field} value={majority} onChange={(e) => setMajority(e.target.value as MajorityRule)}>
                          {(["ordinary", "unanimous", "two_thirds", "four_fifths"] as MajorityRule[]).map((r) => (
                            <option key={r} value={r}>
                              {majorityLabel(r)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button type="button" onClick={() => void saveRules()} className="rounded-lg bg-blue-600 px-3 py-2 text-sm font-semibold text-white hover:bg-blue-700">
                        {t("meetpp.common.save", { defaultValue: "Save" })}
                      </button>
                      <button type="button" onClick={() => setEditRules(false)} className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700">
                        {t("meetpp.common.cancel", { defaultValue: "Cancel" })}
                      </button>
                    </div>
                  )}
                </div>
              )}

              <Section n={4} title={t("meetpp.setup.inputs", { defaultValue: "Inputs (optional)" })}>
                <div className="grid gap-3 sm:grid-cols-2">
                  <DropZone
                    title={t("meetpp.setup.agendaPdf", { defaultValue: "Agenda (PDF, ≤ 10 MB)" })}
                    state={uploads.agenda}
                    onFile={(f) => void upload("agenda", f)}
                    disabled={!sid || template !== "agenda"}
                    summary={(s) =>
                      t("meetpp.setup.agendaSummary", {
                        defaultValue: "{{points}} agenda points · {{subs}} sub-points · {{decisions}} decisions to take",
                        points: s.agenda_points ?? 0,
                        subs: s.subpoints ?? 0,
                        decisions: s.decisions_to_take ?? 0,
                      })
                    }
                  />
                  <DropZone
                    title={t("meetpp.setup.notesPdf", { defaultValue: "Previous meeting notes (PDF)" })}
                    state={uploads.previous_notes}
                    onFile={(f) => void upload("previous_notes", f)}
                    disabled={!sid}
                    hint={importedActions > 0 && !uploads.previous_notes ? t("meetpp.setup.importedFromSeries", { defaultValue: "{{n}} open actions imported from the series", n: importedActions }) : undefined}
                    summary={(s) =>
                      t("meetpp.setup.notesSummary", {
                        defaultValue: "{{actions}} open actions · {{decisions}} decisions · {{members}} members",
                        actions: s.open_actions ?? 0,
                        decisions: s.decisions ?? 0,
                        members: s.roster_members ?? 0,
                      })
                    }
                  />
                </div>
              </Section>

              {template === "agenda" && (
                <Section
                  n={6}
                  title={t("meetpp.setup.editorTitle", { defaultValue: "Agenda (edit before starting — also usable without a PDF)" })}
                  aside={<SaveState state={save} />}
                >
                  <AgendaEditor points={points} update={updatePoints} />
                </Section>
              )}

              <div className="rounded-xl border-2 border-amber-400 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                <b>{t("meetpp.setup.noticeTitle", { defaultValue: "Everyone is asked for consent; the “AI notes” badge stays visible. Audio is deleted when the report is published." })}</b>
                <div className="mt-1">
                  {t("meetpp.setup.notice", {
                    defaultValue: "Live speech-to-text on our server; quality pass on our Mac Studio; text processed by {{provider}}.",
                    provider,
                  })}
                </div>
              </div>
            </div>
          )}
        </div>

        {error && <div className="mx-6 mb-2 rounded bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</div>}
        <div className="flex justify-end gap-2 border-t border-slate-200 px-6 py-3">
          <button type="button" className="rounded-lg border border-slate-300 bg-slate-50 px-5 py-2 text-sm text-slate-700 hover:bg-slate-100" onClick={cancel}>
            {t("meetpp.common.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            type="button"
            data-testid="btn-meetpp-start"
            disabled={busy || loading || !sid}
            onClick={() => void start()}
            className="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t("meetpp.setup.start", { defaultValue: "Start Meet++" })}
          </button>
        </div>
      </div>
    </div>
  );
}

function Section({ n, title, children, aside }: { n: number; title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section aria-label={title}>
      <div className="mb-2 flex items-center gap-2">
        <span className="sr-only">{n}.</span>
        <h3 className="flex-1 text-sm font-semibold text-slate-900">{title}</h3>
        {aside}
      </div>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function SaveState({ state }: { state: "idle" | "pending" | "saving" | "saved" | "error" }) {
  const { t } = useTranslation();
  if (state === "idle") return null;
  const text =
    state === "saving" || state === "pending"
      ? t("meetpp.setup.saving", { defaultValue: "Saving…" })
      : state === "saved"
        ? t("meetpp.setup.saved", { defaultValue: "Saved" })
        : t("meetpp.setup.saveFailed", { defaultValue: "Not saved" });
  return <span className={cx("text-xs", state === "error" ? "text-rose-600" : "text-slate-400")}>{text}</span>;
}

function DropZone({
  title,
  state,
  onFile,
  disabled,
  hint,
  summary,
}: {
  title: string;
  state?: UploadState;
  onFile: (f: File) => void;
  disabled?: boolean;
  hint?: string;
  summary: (s: DocSummary) => string;
}) {
  const { t } = useTranslation();
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f && !disabled) onFile(f);
  };
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={cx("rounded-xl border-2 border-dashed px-4 py-3 text-center", over ? "border-blue-500 bg-blue-50" : "border-slate-300", disabled && "opacity-50")}
    >
      <div className="font-semibold text-slate-800">{title}</div>
      {state && (
        <div
          className={cx(
            "mx-auto mt-2 truncate rounded-lg border-2 px-3 py-1 text-sm",
            state.status === "done" ? "border-emerald-500 bg-emerald-50 text-emerald-800" : state.status === "failed" ? "border-rose-400 bg-rose-50 text-rose-700" : "border-blue-400 bg-blue-50 text-blue-800",
          )}
        >
          {state.status === "done" ? "✓ " : state.status === "failed" ? "✕ " : <Loader2 size={12} className="mr-1 inline animate-spin" />}
          {state.name}
          {state.doc?.page_count ? ` · ${t("meetpp.papers.pages", { defaultValue: "{{n}} pages", n: state.doc.page_count })}` : ""}
          {state.status === "parsing" && ` · ${t("meetpp.setup.parsing", { defaultValue: "reading…" })}`}
          {state.status === "uploading" && ` · ${t("meetpp.setup.uploading", { defaultValue: "uploading…" })}`}
        </div>
      )}
      {state?.status === "done" && state.summary && (
        <div className="mt-1.5 text-sm font-semibold text-blue-700">
          {summary(state.summary)}
          {state.summary.format === "om_report" && (
            <div className="text-xs font-normal text-emerald-700">{t("meetpp.setup.recognised", { defaultValue: "Recognised: OneVoice meeting report" })}</div>
          )}
        </div>
      )}
      {state?.status === "failed" && state.error && <div className="mt-1 text-xs text-rose-600">{state.error}</div>}
      {hint && !state && <div className="mt-1.5 text-sm font-semibold text-blue-700">{hint}</div>}
      <button
        type="button"
        disabled={disabled}
        onClick={() => input.current?.click()}
        className="mt-2 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-800"
      >
        <FileUp size={12} />
        {state ? t("meetpp.setup.replace", { defaultValue: "drop another file or click to replace" }) : t("meetpp.setup.dropHint", { defaultValue: "drop a file or click to browse" })}
      </button>
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) onFile(f);
        }}
      />
    </div>
  );
}

function AgendaEditor({ points, update }: { points: EditorPoint[]; update: (fn: (p: EditorPoint[]) => EditorPoint[]) => void }) {
  const { t } = useTranslation();
  const [dragKey, setDragKey] = useState<string | null>(null);
  const patch = (key: string, p: Partial<EditorPoint>) => update((list) => list.map((x) => (x.key === key ? { ...x, ...p } : x)));
  const move = (from: number, to: number) =>
    update((list) => {
      if (to < 0 || to >= list.length) return list;
      const next = list.slice();
      const [it] = next.splice(from, 1);
      next.splice(to, 0, it);
      return next;
    });
  const letter = (i: number) => String.fromCharCode(97 + (i % 26));
  const input = "rounded border border-slate-200 bg-white px-2 py-1 text-sm text-slate-800 focus:border-blue-400 focus:outline-none";
  return (
    <div className="space-y-1.5">
      {points.length === 0 && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">
          {t("meetpp.setup.editorEmpty", { defaultValue: "No agenda points yet — drop the agenda PDF above or type the points here." })}
        </div>
      )}
      {points.map((p, i) => (
        <div
          key={p.key}
          onDragOver={(e) => dragKey && e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            const from = points.findIndex((x) => x.key === dragKey);
            if (from >= 0 && from !== i) move(from, i);
            setDragKey(null);
          }}
          className={cx("rounded-lg border bg-slate-50", dragKey === p.key ? "border-blue-400 opacity-60" : "border-slate-200")}
        >
          <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5">
            <span
              draggable
              onDragStart={(e) => {
                setDragKey(p.key);
                e.dataTransfer.effectAllowed = "move";
              }}
              onDragEnd={() => setDragKey(null)}
              className="cursor-grab text-slate-400"
              title={t("meetpp.setup.dragHint", { defaultValue: "Drag to reorder" })}
            >
              <GripVertical size={14} />
            </span>
            <span className="w-6 text-right text-sm text-slate-500">{i + 1}.</span>
            <input
              className={cx(input, "min-w-[10rem] flex-1")}
              value={p.title}
              placeholder={t("meetpp.setup.pointTitle", { defaultValue: "Agenda point" })}
              aria-label={t("meetpp.setup.pointTitle", { defaultValue: "Agenda point" })}
              onChange={(e) => patch(p.key, { title: e.target.value })}
            />
            <input
              className={cx(input, "w-32")}
              value={p.presenter}
              placeholder={t("meetpp.setup.presenter", { defaultValue: "Presenter" })}
              aria-label={t("meetpp.setup.presenter", { defaultValue: "Presenter" })}
              onChange={(e) => patch(p.key, { presenter: e.target.value })}
            />
            <input
              className={cx(input, "w-16")}
              type="number"
              min={1}
              value={p.timebox}
              placeholder={t("meetpp.setup.minutes", { defaultValue: "min" })}
              aria-label={t("meetpp.setup.timebox", { defaultValue: "Timebox (minutes)" })}
              onChange={(e) => patch(p.key, { timebox: e.target.value })}
            />
            <button type="button" className="rounded p-1 text-slate-500 hover:bg-slate-200 disabled:opacity-30" disabled={i === 0} onClick={() => move(i, i - 1)} aria-label={t("meetpp.setup.moveUp", { defaultValue: "Move up" })}>
              <ArrowUp size={13} />
            </button>
            <button type="button" className="rounded p-1 text-slate-500 hover:bg-slate-200 disabled:opacity-30" disabled={i === points.length - 1} onClick={() => move(i, i + 1)} aria-label={t("meetpp.setup.moveDown", { defaultValue: "Move down" })}>
              <ArrowDown size={13} />
            </button>
            <button type="button" className="inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-xs text-slate-600 hover:bg-slate-200" onClick={() => patch(p.key, { open: !p.open })} aria-expanded={p.open}>
              {p.open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              {t("meetpp.setup.details", { defaultValue: "{{n}} sub-points", n: p.subpoints.length })}
            </button>
            <button type="button" className="rounded p-1 text-rose-600 hover:bg-rose-50" onClick={() => update((list) => list.filter((x) => x.key !== p.key))} aria-label={t("meetpp.setup.deletePoint", { defaultValue: "Delete point" })}>
              <Trash2 size={13} />
            </button>
          </div>
          {p.open && (
            <div className="space-y-1.5 border-t border-slate-200 px-3 py-2 pl-10">
              <textarea
                rows={2}
                className={cx(input, "w-full")}
                value={p.body}
                placeholder={t("meetpp.setup.description", { defaultValue: "Description (optional)" })}
                aria-label={t("meetpp.setup.description", { defaultValue: "Description (optional)" })}
                onChange={(e) => patch(p.key, { body: e.target.value })}
              />
              {p.subpoints.map((s, j) => (
                <div key={s.key} className="flex items-center gap-1.5">
                  <span className="w-6 text-right text-sm text-slate-500">({letter(j)})</span>
                  <input
                    className={cx(input, "flex-1")}
                    value={s.title}
                    placeholder={t("meetpp.setup.subTitle", { defaultValue: "Sub-point" })}
                    aria-label={t("meetpp.setup.subTitle", { defaultValue: "Sub-point" })}
                    onChange={(e) => patch(p.key, { subpoints: p.subpoints.map((x) => (x.key === s.key ? { ...x, title: e.target.value } : x)) })}
                  />
                  <button type="button" className="rounded p-1 text-rose-600 hover:bg-rose-50" onClick={() => patch(p.key, { subpoints: p.subpoints.filter((x) => x.key !== s.key) })} aria-label={t("meetpp.setup.deleteSub", { defaultValue: "Delete sub-point" })}>
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
              <button type="button" className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 hover:underline" onClick={() => patch(p.key, { subpoints: [...p.subpoints, { key: newKey(), title: "", body: "" }] })}>
                <Plus size={12} /> {t("meetpp.setup.addSub", { defaultValue: "Add sub-point" })}
              </button>
            </div>
          )}
        </div>
      ))}
      <button
        type="button"
        onClick={() => update((list) => [...list, { key: newKey(), title: "", body: "", presenter: "", timebox: "", subpoints: [], open: false }])}
        className="inline-flex items-center gap-1 rounded-lg border border-dashed border-slate-300 px-3 py-1.5 text-sm font-medium text-blue-700 hover:bg-blue-50"
      >
        <Plus size={14} /> {t("meetpp.setup.addPoint", { defaultValue: "Add point" })}
      </button>
    </div>
  );
}
