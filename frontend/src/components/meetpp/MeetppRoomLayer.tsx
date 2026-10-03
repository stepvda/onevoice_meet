import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Bot, ChevronDown, Download, FileUp, Loader2, Sparkles } from "lucide-react";
import { meetppApi } from "../../lib/meetpp/api";
import type { Room } from "livekit-client";
import { useMeetpp } from "../../lib/meetpp/store";
import { usePreferences } from "../../lib/preferences";
import { useMeetppSession } from "./useMeetppSession";
import MeetppBoard from "./MeetppBoard";
import MeetppSubtitles from "./MeetppSubtitles";
import MeetppAnnouncement from "./MeetppAnnouncement";
import MeetppProposal from "./MeetppProposal";

interface Props {
  room: Room;
  roomName: string;
  meetingId: string | null;
  isOwner: boolean;
  roomToken: string | null;
}

type Structure = "agenda" | "goal";

export default function MeetppRoomLayer({ room, roomName, meetingId, isOwner, roomToken }: Props) {
  const { t } = useTranslation();
  const session = useMeetpp((s) => s.session);
  const active = useMeetpp((s) => s.active);
  const announcement = useMeetpp((s) => s.announcement);
  const consent = useMeetpp((s) => s.consent);
  const setAnnouncement = useMeetpp((s) => s.setAnnouncement);
  const speak = usePreferences((s) => s.notifications.speakAnnouncements);
  const [setupOpen, setSetupOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [consentOpen, setConsentOpen] = useState(false);
  const [boardOpen, setBoardOpen] = useState(false);
  const [endSid, setEndSid] = useState<string | null>(null);
  const [editorsOpen, setEditorsOpen] = useState(false);
  const [editorSel, setEditorSel] = useState<string[]>([]);
  const board = useMeetpp((s) => s.board);
  const [menuPos, setMenuPos] = useState<{ top: number; right: number } | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  const openMenu = () => {
    const r = btnRef.current?.getBoundingClientRect();
    if (r) setMenuPos({ top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) });
    setMenuOpen((v) => !v);
  };

  const m = useMeetppSession({ room, roomName, meetingId, isOwner, roomToken });

  const nextPhaseTarget = () => {
    const base = (session?.phase || "opening").split(":")[0];
    return {
      opening: "previous_actions",
      previous_actions: "agenda",
      agenda: "discussion",
      discussion: "discussion:next_item",
      aob: "new_actions",
      new_actions: "closing",
      closing: "closing",
    }[base] ?? "closing";
  };

  const endMeeting = async () => {
    if (!window.confirm(t("meetpp.button.menu.endConfirm", { defaultValue: "End the AI meeting? Minutes and invitations will be prepared." }))) return;
    const id = useMeetpp.getState().session?.id ?? null;
    setBoardOpen(false);
    setMenuOpen(false);
    try {
      await m.endSession();
    } catch {
      /* still show the outputs modal */
    } finally {
      if (id) setEndSid(id);
    }
  };

  // Consent dialog on session start / join during a session.
  useEffect(() => {
    if (!active || consent !== "unknown") return;
    const key = `meetpp-consent:${session?.id ?? ""}`;
    if (sessionStorage.getItem(key)) {
      useMeetpp.getState().setConsent(sessionStorage.getItem(key) === "accept" ? "accepted" : "opted_out");
      return;
    }
    setConsentOpen(true);
  }, [active, consent, session?.id]);

  // Play announcement audio (unless the user muted spoken announcements).
  useEffect(() => {
    if (!announcement) return;
    const ms = (announcement.duration_ms || 2500) + 4000;
    const timer = window.setTimeout(() => setAnnouncement(null), ms);
    if (speak && announcement.audio_url && audioRef.current) {
      audioRef.current.src = announcement.audio_url;
      void audioRef.current.play().catch(() => undefined);
    }
    return () => window.clearTimeout(timer);
  }, [announcement, speak, setAnnouncement]);

  const chosenConsent = (decision: "accept" | "opt_out") => {
    if (session?.id) sessionStorage.setItem(`meetpp-consent:${session.id}`, decision);
    void m.consent(decision);
    setConsentOpen(false);
  };

  if (!isOwner && !active) return null;

  return (
    <>
      {/* AI Meeting button (owner / co-host only). While active, clicking it
          ends the session; the caret opens the secondary menu. */}
      {isOwner && meetingId && (
        <div className="relative flex items-center">
          <button
            ref={btnRef}
            type="button"
            data-testid="btn-ai-meeting"
            onClick={() => (active ? void endMeeting() : setSetupOpen(true))}
            className={[
              "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
              active ? "bg-green-600 text-white hover:bg-green-500" : "bg-primary-700 text-slate-100 hover:bg-primary-600",
            ].join(" ")}
            title={active
              ? t("meetpp.button.endHint", { defaultValue: "AI notes are on — click to end" })
              : t("meetpp.button.aiMeeting", { defaultValue: "AI Meeting" })}
          >
            <Bot size={16} />
            <span className="hidden md:inline">{t("meetpp.button.aiMeeting", { defaultValue: "AI Meeting" })}</span>
          </button>
          {active && (
            <button
              type="button"
              onClick={openMenu}
              aria-label={t("meetpp.button.menu.open", { defaultValue: "AI Meeting options" })}
              className="ml-0.5 rounded-lg bg-primary-700 px-1.5 py-1.5 text-slate-100 hover:bg-primary-600"
            >
              <ChevronDown size={16} />
            </button>
          )}
        </div>
      )}

      {/* Overlays are portaled to <body>: the top bar uses backdrop-blur,
          which establishes a containing block and would otherwise trap these
          `fixed` elements inside the header (the setup modal rendered as a
          thin bar behind the stage). */}
      {typeof document !== "undefined" &&
        createPortal(
          <>
            {/* AI Meeting menu, portaled + fixed so the top bar's overflow and
                backdrop-filter cannot clip or misplace it. */}
            {menuOpen && active && menuPos && (
              <div
                style={{ position: "fixed", top: menuPos.top, right: menuPos.right }}
                className="z-50 w-60 rounded-xl border border-slate-200 bg-white p-1 shadow-2xl"
              >
                <MenuItem onClick={() => { void m.pauseResume(session?.status !== "paused"); setMenuOpen(false); }}>
                  {session?.status === "paused" ? t("meetpp.button.menu.resume", { defaultValue: "Resume transcription" }) : t("meetpp.button.menu.pause", { defaultValue: "Pause transcription" })}
                </MenuItem>
                <MenuItem onClick={() => { void m.jumpPhase(nextPhaseTarget()); setMenuOpen(false); }}>
                  {t("meetpp.button.menu.next", { defaultValue: "Next phase →" })}
                </MenuItem>
                <MenuItem
                  onClick={() => {
                    const id = session?.id;
                    if (id) void meetppApi.patchSession(id, { mode: session?.mode === "lead" ? "assist" : "lead" });
                    setMenuOpen(false);
                  }}
                >
                  {t("meetpp.button.menu.mode", { defaultValue: "Mode" })}:{" "}
                  {session?.mode === "lead"
                    ? t("meetpp.setup.modeLead", { defaultValue: "Lead" })
                    : t("meetpp.setup.modeAssist", { defaultValue: "Assist" })}
                </MenuItem>
                <MenuItem
                  onClick={() => {
                    setEditorSel(session?.editors ?? []);
                    setEditorsOpen(true);
                    setMenuOpen(false);
                  }}
                >
                  {t("meetpp.button.menu.editors", { defaultValue: "Editors…" })}
                </MenuItem>
                <MenuItem onClick={() => { setBoardOpen((v) => !v); setMenuOpen(false); }}>
                  {boardOpen
                    ? t("meetpp.button.menu.hideBoard", { defaultValue: "Hide board" })
                    : t("meetpp.button.menu.showBoard", { defaultValue: "Show board" })}
                </MenuItem>
                <MenuItem onClick={() => { void m.boardToMain(); setMenuOpen(false); }}>
                  {t("meetpp.button.menu.boardToMain", { defaultValue: "Bring board to main" })}
                </MenuItem>
                <MenuItem onClick={() => { setMenuOpen(false); void m.snapshot(); }}>
                  {t("meetpp.button.menu.snapshot", { defaultValue: "Whiteboard snapshot" })}
                </MenuItem>
                <MenuItem danger onClick={() => { void endMeeting(); }}>
                  {t("meetpp.button.menu.end", { defaultValue: "End AI meeting" })}
                </MenuItem>
              </div>
            )}

            {/* Speech-paced scrolling subtitles */}
            <MeetppSubtitles />

            {/* "AI notes on" purple badge, top-left of the stage */}
            {active && (
              <div data-testid="ai-notes-badge" className="fixed left-4 top-20 z-30 inline-flex items-center gap-1.5 rounded-full bg-purple-600 px-3 py-1 text-xs font-medium text-white shadow-lg">
                <Sparkles size={13} />
                {session?.status === "paused"
                  ? t("meetpp.button.aiNotesPaused", { defaultValue: "AI notes paused" })
                  : t("meetpp.button.aiNotesOn", { defaultValue: "AI notes on" })}
              </div>
            )}

            {/* Big-text phase announcement */}
            <MeetppAnnouncement />

            {/* Chair-only proposal card */}
            {isOwner && active && (
              <MeetppProposal
                onAccept={(p) => void m.acceptProposal({ ...p, pid: "", reason: "", confidence: 0, auto_at: null })}
                onReject={(p) => void m.rejectProposal({ ...p, pid: "", reason: "", confidence: 0, auto_at: null })}
              />
            )}

            {/* Consent dialog */}
            {consentOpen && (
              // Must be answered: no backdrop dismissal, otherwise transcription
              // stays off with no way to re-open the dialog.
              <Modal onClose={() => undefined} title={t("meetpp.consent.title", { defaultValue: "AI notes in this meeting" })}>
                <p className="text-sm text-slate-600">
                  {t("meetpp.consent.body", { provider: "the configured provider" })}
                </p>
                <div className="mt-4 flex justify-end gap-2">
                  <button className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700" onClick={() => chosenConsent("opt_out")}>
                    {t("meetpp.consent.optOut", { defaultValue: "Don't transcribe me" })}
                  </button>
                  <button className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-700" onClick={() => chosenConsent("accept")}>
                    {t("meetpp.consent.continue", { defaultValue: "Continue" })}
                  </button>
                </div>
              </Modal>
            )}

            {/* Setup modal */}
            {setupOpen && (
              <SetupModal
                onClose={() => setSetupOpen(false)}
                onStart={async (body, files) => {
                  await m.startSession(body, files);
                  setSetupOpen(false);
                }}
                loading={m.loading}
              />
            )}

            {/* Editable board for the chair/co-host. The stage tile is
                read-only for everyone; this panel is where humans confirm,
                edit, reject and add. */}
            {isOwner && active && boardOpen && (
              <div className="fixed inset-y-0 right-0 z-30 flex w-full max-w-3xl flex-col bg-slate-100 shadow-2xl">
                <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
                  <span className="text-sm font-semibold text-slate-800">
                    {t("meetpp.button.aiMeeting", { defaultValue: "AI Meeting" })}
                  </span>
                  <button className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700" onClick={() => setBoardOpen(false)}>
                    {t("meetpp.announce.dismiss", { defaultValue: "Close" })}
                  </button>
                </div>
                <div className="min-h-0 flex-1 p-3">
                  <MeetppBoard
                    canEdit
                    onSendOps={m.sendOps}
                    onJumpPhase={m.jumpPhase}
                    onAddSnapshot={m.snapshot}
                    className="h-full w-full"
                  />
                </div>
              </div>
            )}

            {/* Editor picker */}
            {editorsOpen && (
              <Modal title={t("meetpp.button.menu.editors", { defaultValue: "Editors" })} onClose={() => setEditorsOpen(false)}>
                <p className="mb-2 text-sm text-slate-500">
                  {t("meetpp.editors.hint", { defaultValue: "Editors can confirm, edit, reject and add board items." })}
                </p>
                <div className="max-h-64 overflow-y-auto">
                  {(board?.attendance ?? []).flatMap((a) => {
                    const identities = a.identities && a.identities.length ? a.identities : [a.person_key];
                    return identities.map((ident) => (
                      <label key={`${a.id}-${ident}`} className="flex items-center gap-2 py-1 text-sm text-slate-700">
                        <input
                          type="checkbox"
                          checked={editorSel.includes(ident)}
                          onChange={(e) =>
                            setEditorSel((prev) => (e.target.checked ? [...prev, ident] : prev.filter((x) => x !== ident)))
                          }
                        />
                        {a.name} <span className="text-xs text-slate-400">{ident}</span>
                      </label>
                    ));
                  })}
                  {(!board || board.attendance.length === 0) && (
                    <div className="py-2 text-xs text-slate-400">{t("meetpp.editors.none", { defaultValue: "No participants yet." })}</div>
                  )}
                </div>
                <div className="mt-3 flex justify-end gap-2">
                  <button className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-700" onClick={() => setEditorsOpen(false)}>
                    {t("meetpp.setup.cancel", { defaultValue: "Cancel" })}
                  </button>
                  <button
                    className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white"
                    onClick={() => {
                      const id = session?.id;
                      if (id) void meetppApi.patchSession(id, { editors: editorSel });
                      setEditorsOpen(false);
                    }}
                  >
                    {t("meetpp.review.saveDraft", { defaultValue: "Save" })}
                  </button>
                </div>
              </Modal>
            )}

            {/* End-of-meeting outputs */}
            {endSid && <EndOutputsModal sid={endSid} onClose={() => setEndSid(null)} />}
          </>,
          document.body,
        )}
      <audio ref={audioRef} className="hidden" />
    </>
  );
}

function MenuItem({ children, onClick, danger }: { children: ReactNode; onClick: () => void; danger?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "block w-full rounded-lg px-2.5 py-1.5 text-left text-sm",
        danger ? "text-rose-600 hover:bg-rose-50" : "text-slate-700 hover:bg-slate-100",
      ].join(" ")}
    >
      {children}
    </button>
  );
}

function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-2xl bg-white p-5 text-slate-800 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 text-xl font-bold text-slate-900">{title}</div>
        {children}
      </div>
    </div>
  );
}

function SetupModal({
  onClose,
  onStart,
  loading,
}: {
  onClose: () => void;
  onStart: (
    body: { template: string; mode: string; language: string; series_id?: string | null; goal?: string | null },
    files?: { agenda?: File | null; previousNotes?: File | null },
  ) => Promise<void>;
  loading: boolean;
}) {
  const { t } = useTranslation();
  const [structure, setStructure] = useState<Structure>("agenda");
  const [language, setLanguage] = useState("en");
  const [mode, setMode] = useState("lead");
  const [goal, setGoal] = useState("");
  const [agendaFile, setAgendaFile] = useState<File | null>(null);
  const [notesFile, setNotesFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  const field = "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800";

  return (
    <Modal title={t("meetpp.setup.title", { defaultValue: "Start AI Meeting" })} onClose={onClose}>
      <p className="mb-4 text-sm text-slate-500">
        {t("meetpp.setup.intro", { defaultValue: "Meet++ will transcribe the meeting, keep the agenda, decisions, actions and minutes up to date, and prepare the follow-up." })}
      </p>

      <div className="mb-4">
        <div className="mb-2 text-sm font-semibold text-slate-800">{t("meetpp.setup.structure", { defaultValue: "Meeting structure" })}</div>
        <div className="grid grid-cols-2 gap-3">
          {(["agenda", "goal"] as Structure[]).map((s2) => (
            <button
              key={s2}
              type="button"
              onClick={() => setStructure(s2)}
              className={["flex items-start gap-2 rounded-xl border p-3 text-left", structure === s2 ? "border-blue-600 bg-blue-50" : "border-slate-200 bg-white"].join(" ")}
            >
              <span className={["mt-0.5 grid h-4 w-4 place-items-center rounded-full border-2", structure === s2 ? "border-blue-600" : "border-slate-300"].join(" ")}>
                {structure === s2 && <span className="h-2 w-2 rounded-full bg-blue-600" />}
              </span>
              <span>
                <span className="block text-sm font-semibold text-slate-800">{t(`meetpp.setup.${s2}Driven`, { defaultValue: s2 === "agenda" ? "Agenda-driven" : "Goal-driven" })}</span>
                <span className="block text-xs text-slate-500">{t(`meetpp.setup.${s2}DrivenHint`, { defaultValue: s2 === "agenda" ? "review actions → agenda → AOB → new actions" : "goal → in-meeting deliverables → next steps → planning" })}</span>
              </span>
            </button>
          ))}
        </div>
      </div>

      {structure === "goal" && (
        <input className={field + " mb-4"} placeholder={t("meetpp.setup.goalPlaceholder", { defaultValue: "Goal" })} value={goal} onChange={(e) => setGoal(e.target.value)} />
      )}

      <div className="mb-4 grid grid-cols-3 gap-3">
        <label className="block">
          <span className="text-xs font-medium text-slate-500">{t("meetpp.setup.language", { defaultValue: "Spoken language" })}</span>
          <select className={field} value={language} onChange={(e) => setLanguage(e.target.value)}>
            {[["en", "English (en)"], ["nl", "Nederlands (nl)"], ["fr", "Français (fr)"], ["de", "Deutsch (de)"]].map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-500">{t("meetpp.setup.mode", { defaultValue: "AI mode" })}</span>
          <select className={field} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="lead">{t("meetpp.setup.modeLeadLong", { defaultValue: "Lead — AI moves on, chair can cancel" })}</option>
            <option value="assist">{t("meetpp.setup.modeAssist", { defaultValue: "Assist — chair confirms each transition" })}</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-500">{t("meetpp.setup.series", { defaultValue: "Meeting series" })}</span>
          <select className={field} defaultValue="this">
            <option value="this">{t("meetpp.setup.thisSeries", { defaultValue: "This meeting's series" })}</option>
            <option value="new">{t("meetpp.setup.newSeries", { defaultValue: "New series" })}</option>
          </select>
        </label>
      </div>

      <div className="mb-4">
        <div className="mb-2 text-sm font-semibold text-slate-800">{t("meetpp.setup.inputs", { defaultValue: "Inputs (optional)" })}</div>
        <div className="grid grid-cols-2 gap-3">
          <label className="block rounded-xl border-2 border-dashed border-slate-300 bg-slate-50 px-3 py-3 text-center text-xs text-slate-500">
            <span className="mb-1 block font-medium text-slate-700">{t("meetpp.setup.agendaPdfZone", { defaultValue: "Agenda (PDF, ≤ 10 MB)" })}</span>
            <input type="file" accept="application/pdf" className="mx-auto block w-full text-[11px] text-slate-500 file:mr-2 file:rounded file:border-0 file:bg-blue-600 file:px-2 file:py-1 file:text-white" onChange={(e) => setAgendaFile(e.target.files?.[0] ?? null)} />
            {agendaFile ? <span className="mt-2 block truncate rounded border border-emerald-300 bg-emerald-50 px-2 py-1 text-emerald-700">✓ {agendaFile.name}</span> : <span className="mt-2 block">{t("meetpp.setup.dropHint", { defaultValue: "drop a file or click to browse" })}</span>}
          </label>
          <label className="block rounded-xl border-2 border-dashed border-slate-300 bg-slate-50 px-3 py-3 text-center text-xs text-slate-500">
            <span className="mb-1 block font-medium text-slate-700">{t("meetpp.setup.previousNotesPdfZone", { defaultValue: "Previous meeting notes (PDF)" })}</span>
            <input type="file" accept="application/pdf" className="mx-auto block w-full text-[11px] text-slate-500 file:mr-2 file:rounded file:border-0 file:bg-blue-600 file:px-2 file:py-1 file:text-white" onChange={(e) => setNotesFile(e.target.files?.[0] ?? null)} />
            {notesFile ? <span className="mt-2 block truncate rounded border border-blue-300 bg-blue-50 px-2 py-1 text-blue-700">↻ {notesFile.name}</span> : <span className="mt-2 block">{t("meetpp.setup.importedHint", { defaultValue: "Auto-imported from the series when available" })}</span>}
          </label>
        </div>
      </div>

      <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
        <b>{t("meetpp.setup.noticeTitle", { defaultValue: "Everyone in the room is notified and asked for consent; an “AI notes” badge stays visible." })}</b>
        <div className="mt-0.5">{t("meetpp.setup.notice", { defaultValue: "Speech is transcribed on our server; the text is processed by the configured LLM provider to produce minutes." })}</div>
      </div>

      {error && <div className="mb-3 rounded bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</div>}

      <div className="flex justify-end gap-2">
        <button className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700" onClick={onClose}>
          {t("meetpp.setup.cancel", { defaultValue: "Cancel" })}
        </button>
        <button
          className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
          disabled={loading}
          onClick={async () => {
            setError(null);
            try {
              await onStart(
                { template: structure, mode, language, goal: structure === "goal" ? goal : null },
                { agenda: agendaFile, previousNotes: notesFile },
              );
            } catch (e) {
              setError(e instanceof Error ? e.message : "Could not start");
            }
          }}
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <FileUp size={14} />}
          {t("meetpp.setup.startAi", { defaultValue: "Start AI Meeting" })}
        </button>
      </div>
    </Modal>
  );
}

interface OutputsState {
  status: string;
  finalised: boolean;
  ready: boolean;
  outputs: Array<{ id: string; kind: string; filename: string; download_url: string }>;
  auto_sent: boolean;
  recurring: boolean;
}

const OUTPUT_LABELS: Record<string, string> = {
  minutes_pdf: "Meeting notes (PDF)",
  agenda_pdf: "Proposed agenda / next meeting (PDF)",
  ics: "Calendar invite (.ics)",
};

function EndOutputsModal({ sid, onClose }: { sid: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [data, setData] = useState<OutputsState | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let stopped = false;
    const startedAt = Date.now();
    const load = async () => {
      try {
        const r = (await meetppApi.getOutputs(sid)) as OutputsState;
        if (stopped) return;
        setData(r);
        if (!r.ready && Date.now() - startedAt > 60000) setFailed(true);
      } catch {
        if (!stopped) setFailed(true);
      }
    };
    void load();
    const timer = window.setInterval(() => {
      void load();
    }, 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [sid]);

  const readyKinds = new Set((data?.outputs ?? []).map((o) => o.kind));
  const required = ["minutes_pdf", "agenda_pdf", "ics"];
  const readyCount = required.filter((k) => readyKinds.has(k)).length;
  const pct = Math.round((readyCount / required.length) * 100);
  const done = data?.ready ?? false;

  const download = async (o: { id: string; filename: string; download_url: string }) => {
    const { getAccessToken } = await import("../../lib/auth");
    const token = getAccessToken();
    const res = await fetch(o.download_url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = o.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Modal title={t("meetpp.review.endTitle", { defaultValue: "AI meeting ended — preparing outputs" })} onClose={onClose}>
      {!done && !failed && (
        <div className="space-y-3">
          <p className="text-sm text-slate-300">
            {t("meetpp.review.preparing", { defaultValue: "Finalising minutes and the next-meeting draft…" })}
          </p>
          <div className="h-2 w-full overflow-hidden rounded bg-primary-800">
            <div className="h-full bg-accent-500 transition-all" style={{ width: `${pct}%` }} />
          </div>
          <div className="text-xs text-slate-400">{readyCount}/3</div>
        </div>
      )}

      {failed && (
        <p className="text-sm text-rose-300">
          {t("meetpp.review.outputsFailed", { defaultValue: "Outputs are not ready yet. You can retry from the review screen." })}
        </p>
      )}

      {done && data && (
        <div className="space-y-3">
          <p className="text-sm text-slate-300">
            {t("meetpp.review.outputsReady", { defaultValue: "Everything is ready. Download the outputs:" })}
          </p>
          <div className="space-y-2">
            {data.outputs.map((o) => (
              <button
                key={o.id}
                type="button"
                onClick={() => void download(o)}
                className="flex w-full items-center gap-2 rounded border border-primary-700 bg-primary-950 px-3 py-2 text-left text-sm text-slate-100 hover:bg-primary-800"
              >
                <Download size={15} className="text-accent-400" />
                <span>{OUTPUT_LABELS[o.kind] ?? o.filename}</span>
              </button>
            ))}
          </div>
          {data.auto_sent ? (
            <div className="rounded bg-emerald-900/40 px-3 py-2 text-xs text-emerald-200">
              {t("meetpp.review.invitesSent", { defaultValue: "Invitations for the next meeting were sent by e-mail to the required attendees." })}
            </div>
          ) : data.recurring ? (
            <div className="rounded bg-primary-800 px-3 py-2 text-xs text-slate-300">
              {t("meetpp.review.invitesPending", { defaultValue: "Invitations could not be sent automatically; send them from the review screen." })}
            </div>
          ) : (
            <div className="rounded bg-primary-800 px-3 py-2 text-xs text-slate-300">
              {t("meetpp.review.reviewLink", { defaultValue: "Open the review screen to edit and send the minutes and invitations." })}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

