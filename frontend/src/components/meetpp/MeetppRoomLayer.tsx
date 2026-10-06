import { useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { ChevronDown, ExternalLink, Sparkles } from "lucide-react";
import type { Room } from "livekit-client";
import { api } from "../../lib/api";
import { chooseConsent, meetppActions, reopenConsent } from "../../lib/meetpp/session";
import { setEndRequest, useMeetpp } from "../../lib/meetpp/store";
import { BOARD_KEY } from "../../lib/stage";
import { setFocus } from "../../lib/stageView";
import { useMeetppRoom } from "./useMeetppSession";
import MeetppSetup from "./MeetppSetup";
import { cx, useOnClickOutside } from "./ui";

interface Props {
  room: Room;
  roomName: string;
  meetingId: string | null;
  isOwner: boolean;
  roomToken: string | null;
}

/**
 * Meet++ in the room top bar: the chair's Meet++ button (setup / session
 * menu), the "AI notes" badge for everyone, the consent dialog and the end
 * dialog. The board itself lives on the stage (PresenterSpotlight); there is
 * no second, owner-only board.
 */
export default function MeetppRoomLayer({ room, roomName, meetingId, isOwner, roomToken }: Props) {
  const { t } = useTranslation();
  useMeetppRoom({ room, roomName, meetingId, isChair: isOwner, roomToken });
  const active = useMeetpp((s) => s.active);
  const session = useMeetpp((s) => s.snap?.session ?? null);
  const consent = useMeetpp((s) => s.consent);
  const consentPrompt = useMeetpp((s) => s.consentPrompt);
  const providerLabel = useMeetpp((s) => s.providerLabel);
  // The board is a stream window (lib/stage.ts): "Open the board" zooms it
  // in this viewer's own view; the chair can make it the presenter again.
  const boardOnStage = useMeetpp((s) => s.boardOnStage);
  const boardPresenter = useMeetpp((s) => s.boardPresenter);
  const [setupOpen, setSetupOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [badgeOpen, setBadgeOpen] = useState(false);
  const [editorsOpen, setEditorsOpen] = useState(false);
  const [endedSid, setEndedSid] = useState<string | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; right: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const badgeRef = useRef<HTMLDivElement>(null);
  useOnClickOutside(badgeRef, () => setBadgeOpen(false), badgeOpen);

  const run = async (p: Promise<unknown>) => {
    setErr(null);
    try {
      await p;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const openMenu = () => {
    const r = btnRef.current?.getBoundingClientRect();
    if (r) setMenuPos({ top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) });
    setMenuOpen((v) => !v);
  };

  // "End this meeting" heard from the chair: confirm in a dialog (v3.2).
  const endRequest = useMeetpp((s) => s.endRequest);
  const [ending, setEnding] = useState(false);
  const confirmSpokenEnd = async () => {
    setEnding(true);
    try {
      const sid = await meetppActions.end();
      setEndRequest(null);
      setEndedSid(sid);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setEnding(false);
    }
  };

  const endMeeting = async () => {
    setMenuOpen(false);
    if (!window.confirm(t("meetpp.menu.endConfirm", { defaultValue: "End Meet++? The minutes and the report will be prepared for your review." }))) return;
    try {
      const sid = await meetppActions.end();
      setEndedSid(sid);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const settings = session?.settings ?? {};
  const toggleSetting = (key: "show_public" | "in_recordings" | "speak" | "timebox_nudges", fallback: boolean) =>
    void run(meetppActions.setSettings({ ...settings, [key]: !(settings[key] ?? fallback) }));

  if (!isOwner && !active) return null;

  return (
    <>
      {isOwner && meetingId && (
        <div className="relative flex items-center">
          <button
            ref={btnRef}
            type="button"
            data-testid="btn-ai-meeting"
            onClick={() => (active ? openMenu() : setSetupOpen(true))}
            aria-haspopup={active ? "menu" : "dialog"}
            className={cx(
              "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-semibold",
              active ? "bg-green-600 text-white hover:bg-green-500" : "bg-primary-700 text-slate-100 hover:bg-primary-600",
            )}
            title={active ? t("meetpp.menu.title", { defaultValue: "Meet++ options" }) : t("meetpp.menu.start", { defaultValue: "Start Meet++" })}
          >
            <Sparkles size={15} />
            <span className="hidden md:inline">Meet++</span>
            {active && <ChevronDown size={14} />}
          </button>
        </div>
      )}

      {active && (
        <div ref={badgeRef} className="relative">
          <button
            type="button"
            data-testid="ai-notes-badge"
            onClick={() => setBadgeOpen((v) => !v)}
            className={cx(
              "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium text-white",
              consent === "opt_out" ? "bg-slate-500" : "bg-purple-600",
            )}
          >
            <Sparkles size={12} />
            {session?.status === "paused"
              ? t("meetpp.badge.paused", { defaultValue: "AI notes paused" })
              : consent === "opt_out"
                ? t("meetpp.badge.notTranscribed", { defaultValue: "AI notes · not transcribed" })
                : t("meetpp.badge.on", { defaultValue: "AI notes on" })}
          </button>
          {badgeOpen && (
            <div className="absolute left-0 top-full z-50 mt-1 w-72 rounded-xl border border-slate-200 bg-white p-3 text-sm text-slate-700 shadow-2xl">
              <p>
                {consent === "opt_out"
                  ? t("meetpp.badge.youOptedOut", { defaultValue: "Your speech is not transcribed." })
                  : consent === "accept"
                    ? t("meetpp.badge.youAccepted", { defaultValue: "Your speech is transcribed for the minutes." })
                    : t("meetpp.badge.undecided", { defaultValue: "You have not chosen yet; you are not transcribed." })}
              </p>
              <button
                type="button"
                className="mt-2 text-sm font-semibold text-blue-700 hover:underline"
                onClick={() => {
                  setBadgeOpen(false);
                  reopenConsent();
                }}
              >
                {t("meetpp.consent.change", { defaultValue: "Change my choice" })}
              </button>
              {!boardOnStage && (
                <button
                  type="button"
                  className="mt-1 block text-sm font-semibold text-blue-700 hover:underline"
                  onClick={() => {
                    setBadgeOpen(false);
                    setFocus(BOARD_KEY);
                  }}
                >
                  {t("meetpp.stage.open", { defaultValue: "Open the board" })}
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {typeof document !== "undefined" &&
        createPortal(
          <>
            {menuOpen && active && menuPos && (
              <MenuPanel pos={menuPos} onClose={() => setMenuOpen(false)}>
                <MenuItem onClick={() => void run(session?.status === "paused" ? meetppActions.resume() : meetppActions.pause())}>
                  {session?.status === "paused"
                    ? t("meetpp.menu.resume", { defaultValue: "Resume transcription" })
                    : t("meetpp.menu.pause", { defaultValue: "Pause transcription" })}
                </MenuItem>
                <MenuItem onClick={() => void run(meetppActions.setMode(session?.mode === "lead" ? "assist" : "lead"))}>
                  {session?.mode === "lead"
                    ? t("meetpp.menu.modeLead", { defaultValue: "AI mode: Lead (switch to Assist)" })
                    : t("meetpp.menu.modeAssist", { defaultValue: "AI mode: Assist (switch to Lead)" })}
                </MenuItem>
                <MenuToggle on={settings.speak !== false} onClick={() => toggleSetting("speak", true)}>
                  {t("meetpp.menu.speak", { defaultValue: "Spoken announcements" })}
                </MenuToggle>
                <MenuToggle on={settings.timebox_nudges !== false} onClick={() => toggleSetting("timebox_nudges", true)}>
                  {t("meetpp.menu.nudges", { defaultValue: "Timebox nudges" })}
                </MenuToggle>
                <MenuToggle on={settings.in_recordings !== false} onClick={() => toggleSetting("in_recordings", true)}>
                  {t("meetpp.menu.inRecordings", { defaultValue: "Board in recordings" })}
                </MenuToggle>
                <MenuToggle on={settings.show_public === true} onClick={() => toggleSetting("show_public", false)}>
                  {t("meetpp.menu.showPublic", { defaultValue: "Board on the public view" })}
                </MenuToggle>
                <MenuItem onClick={() => setEditorsOpen(true)}>{t("meetpp.menu.editors", { defaultValue: "Editors…" })}</MenuItem>
                <MenuItem onClick={() => void run(meetppActions.snapshotWhiteboard(roomName))}>{t("meetpp.menu.snapshot", { defaultValue: "Whiteboard snapshot" })}</MenuItem>
                {!boardPresenter && meetingId && (
                  <MenuItem onClick={() => void run(api.setPresenter(meetingId, BOARD_KEY))}>{t("meetpp.stage.present", { defaultValue: "Present the board" })}</MenuItem>
                )}
                {!boardOnStage && <MenuItem onClick={() => setFocus(BOARD_KEY)}>{t("meetpp.stage.open", { defaultValue: "Open the board" })}</MenuItem>}
                <MenuItem danger onClick={() => void endMeeting()}>
                  {t("meetpp.menu.end", { defaultValue: "End Meet++" })}
                </MenuItem>
              </MenuPanel>
            )}

            {endRequest && active && isOwner && (
              <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 p-4" data-testid="meetpp-end-request">
                <div role="dialog" aria-modal="true" aria-labelledby="meetpp-end-title" className="w-full max-w-md rounded-2xl bg-white p-5 text-slate-800 shadow-2xl">
                  <h2 id="meetpp-end-title" className="text-lg font-semibold">
                    {t("meetpp.endRequest.title", { defaultValue: "End the meeting?" })}
                  </h2>
                  <p className="mt-2 text-sm text-slate-600">
                    {t("meetpp.endRequest.heard", { defaultValue: "Heard: “{{text}}”", text: endRequest.heard })}
                  </p>
                  <p className="mt-2 text-sm">
                    {t("meetpp.endRequest.body", { defaultValue: "Meet++ stops taking notes and prepares the minutes and the report for your review." })}
                  </p>
                  <div className="mt-4 flex justify-end gap-2">
                    <button type="button" autoFocus className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-50" onClick={() => setEndRequest(null)}>
                      {t("meetpp.endRequest.keep", { defaultValue: "Keep going" })}
                    </button>
                    <button type="button" disabled={ending} className="rounded-lg bg-rose-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-rose-700 disabled:opacity-60" onClick={() => void confirmSpokenEnd()}>
                      {t("meetpp.endRequest.end", { defaultValue: "End the meeting" })}
                    </button>
                  </div>
                </div>
              </div>
            )}

            {err && (
              <div className="fixed right-4 top-16 z-[60] max-w-sm rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800 shadow-lg" role="alert" onClick={() => setErr(null)}>
                {err}
              </div>
            )}

            {consentPrompt && active && (
              <Dialog title={t("meetpp.consent.title", { defaultValue: "AI notes in this meeting" })}>
                <p className="text-sm text-slate-600">
                  {t("meetpp.consent.body", {
                    defaultValue:
                      "This meeting uses Meet++ AI notes. Your speech is transcribed live on our server and refined on the association's Mac Studio; the text is processed by {{provider}} to keep the agenda, decisions, actions and minutes.",
                    provider: providerLabel || t("meetpp.setup.providerDefault", { defaultValue: "the configured AI provider" }),
                  })}
                </p>
                <p className="mt-2 text-sm text-slate-600">
                  {t("meetpp.consent.audio", { defaultValue: "Recorded audio is deleted when the report is published (at the latest after 7 days). You can change your choice at any time from the “AI notes” badge." })}
                </p>
                <div className="mt-4 flex justify-end gap-2">
                  <button type="button" className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50" onClick={() => void chooseConsent("opt_out")}>
                    {t("meetpp.consent.optOut", { defaultValue: "Don't transcribe me" })}
                  </button>
                  <button type="button" className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-700" onClick={() => void chooseConsent("accept")}>
                    {t("meetpp.consent.continue", { defaultValue: "Continue" })}
                  </button>
                </div>
              </Dialog>
            )}

            {setupOpen && meetingId && (
              <MeetppSetup
                meetingId={meetingId}
                roomName={roomName}
                onClose={() => setSetupOpen(false)}
                onStarted={() => setSetupOpen(false)}
              />
            )}

            {editorsOpen && <EditorsDialog onClose={() => setEditorsOpen(false)} />}

            {endedSid && meetingId && (
              <Dialog title={t("meetpp.end.title", { defaultValue: "Meet++ ended — preparing the report" })}>
                <p className="text-sm text-slate-600">
                  {t("meetpp.end.body", {
                    defaultValue:
                      "The whole meeting is being re-transcribed and the minutes composed. Review the report, votes and attendance, then publish. Nothing is sent before you click Publish & send.",
                  })}
                </p>
                <div className="mt-4 flex justify-end gap-2">
                  <button type="button" className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-700" onClick={() => setEndedSid(null)}>
                    {t("meetpp.common.close", { defaultValue: "Close" })}
                  </button>
                  <a
                    href={`/meetings/${meetingId}/ai/${endedSid}`}
                    target="_blank"
                    rel="noreferrer"
                    onClick={() => setEndedSid(null)}
                    className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-700"
                  >
                    {t("meetpp.end.openReview", { defaultValue: "Open review" })} <ExternalLink size={13} />
                  </a>
                </div>
              </Dialog>
            )}
          </>,
          document.body,
        )}
    </>
  );
}

function MenuPanel({ pos, onClose, children }: { pos: { top: number; right: number }; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useOnClickOutside(ref, onClose);
  return (
    <div ref={ref} role="menu" style={{ position: "fixed", top: pos.top, right: pos.right }} className="z-50 w-64 rounded-xl border border-slate-200 bg-white p-1 shadow-2xl" onClick={onClose}>
      {children}
    </div>
  );
}

function MenuItem({ children, onClick, danger }: { children: ReactNode; onClick: () => void; danger?: boolean }) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={cx("block w-full rounded-lg px-2.5 py-1.5 text-left text-sm", danger ? "text-rose-600 hover:bg-rose-50" : "text-slate-700 hover:bg-slate-100")}
    >
      {children}
    </button>
  );
}

function MenuToggle({ children, on, onClick }: { children: ReactNode; on: boolean; onClick: () => void }) {
  return (
    <button type="button" role="menuitemcheckbox" aria-checked={on} onClick={onClick} className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100">
      <span className={cx("relative h-4 w-7 flex-shrink-0 rounded-full transition-colors", on ? "bg-emerald-500" : "bg-slate-300")}>
        <span className={cx("absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all", on ? "left-3.5" : "left-0.5")} />
      </span>
      {children}
    </button>
  );
}

function Dialog({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl bg-white p-5 text-slate-800 shadow-2xl">
        <div className="mb-3 text-xl font-bold text-slate-900">{title}</div>
        {children}
      </div>
    </div>
  );
}

/** Designated editors (session.editors = LiveKit identities). */
function EditorsDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const editors = useMeetpp((s) => s.snap?.session.editors ?? []);
  const attendees = useMeetpp((s) => s.snap?.attendees ?? []);
  const self = useMeetpp((s) => s.ctx.localIdentity);
  const [sel, setSel] = useState<string[]>(editors);
  const [busy, setBusy] = useState(false);
  // Signed-in attendees map to `user-<sub>` identities; guests cannot be editors.
  const candidates = attendees
    .flatMap((a) => (a.person_key?.startsWith("sub:") ? [{ identity: `user-${a.person_key.slice(4)}`, name: a.name }] : []))
    .filter((c) => c.identity !== self);
  return (
    <Dialog title={t("meetpp.editors.title", { defaultValue: "Editors" })}>
      <p className="mb-2 text-sm text-slate-500">{t("meetpp.editors.hint", { defaultValue: "Editors can confirm, edit, reject and add items on the board. Only the chair and co-hosts move the meeting." })}</p>
      <div className="max-h-64 overflow-y-auto">
        {candidates.length === 0 && <div className="py-2 text-sm text-slate-400">{t("meetpp.editors.none", { defaultValue: "No signed-in participants yet." })}</div>}
        {candidates.map((c) => (
          <label key={c.identity} className="flex items-center gap-2 py-1 text-sm text-slate-700">
            <input type="checkbox" checked={sel.includes(c.identity)} onChange={(e) => setSel((p) => (e.target.checked ? [...p, c.identity] : p.filter((x) => x !== c.identity)))} />
            {c.name}
          </label>
        ))}
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <button type="button" className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-700" onClick={onClose}>
          {t("meetpp.common.cancel", { defaultValue: "Cancel" })}
        </button>
        <button
          type="button"
          disabled={busy}
          className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-60"
          onClick={async () => {
            setBusy(true);
            try {
              await meetppActions.setEditors(sel);
              onClose();
            } finally {
              setBusy(false);
            }
          }}
        >
          {t("meetpp.common.save", { defaultValue: "Save" })}
        </button>
      </div>
    </Dialog>
  );
}
