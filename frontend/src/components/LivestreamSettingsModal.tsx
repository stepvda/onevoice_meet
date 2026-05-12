import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Radio, X } from "lucide-react";
import { api, MeetingOut } from "../lib/api";
import { Button, Card, Field, Input, Toggle } from "./ui";

interface Props {
  meeting: MeetingOut;
  open: boolean;
  onClose: () => void;
  onSaved: (updated: MeetingOut) => void;
}

/**
 * Edit the X.com livestream config on an existing meeting. Used both from
 * MyMeetings (Account / Home page) and from the in-meeting toolbar so the
 * host can paste new credentials mid-call without leaving the room.
 */
export default function LivestreamSettingsModal({ meeting, open, onClose, onSaved }: Props) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(!!meeting.livestream_enabled);
  const [url, setUrl] = useState(meeting.livestream_rtmps_url ?? "");
  const [key, setKey] = useState(meeting.livestream_stream_key ?? "");
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Re-seed when the modal is reopened against a different meeting.
  useEffect(() => {
    if (!open) return;
    setEnabled(!!meeting.livestream_enabled);
    setUrl(meeting.livestream_rtmps_url ?? "");
    setKey(meeting.livestream_stream_key ?? "");
    setShowKey(false);
    setErr(null);
  }, [open, meeting]);

  if (!open) return null;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const updated = await api.updateMeeting(meeting.id, {
        livestream_enabled: enabled,
        livestream_rtmps_url: url.trim() || null,
        livestream_stream_key: key.trim() || null,
      });
      onSaved(updated);
      onClose();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      data-testid="livestream-modal"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm pt-16 px-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("livestream.modalTitle", { defaultValue: "Configure live stream" })}
    >
      <Card className="w-full max-w-lg relative">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close", { defaultValue: "Close" })}
          data-testid="livestream-modal-close"
          className="absolute top-3 right-3 p-1 rounded-md text-slate-300 hover:bg-primary-700"
        >
          <X size={18} />
        </button>
        <h2 className="text-lg font-semibold mb-1 flex items-center gap-2 text-slate-50">
          <Radio size={18} className="text-accent-500" />
          {t("livestream.modalTitle", { defaultValue: "Configure live stream" })}
        </h2>
        <p className="text-sm text-slate-400 mb-4">
          {t("livestream.modalMeeting", { defaultValue: "Meeting:" })}{" "}
          <span className="text-slate-200">{meeting.display_title}</span>
        </p>

        <form onSubmit={save} className="flex flex-col gap-4">
          <Toggle
            id="ls-enabled"
            label={t("createMeeting.livestreamEnableX", { defaultValue: "Stream this meeting live to X.com" })}
            description={t("createMeeting.livestreamEnableDesc", {
              defaultValue:
                "When on, the host gets a Start/Stop streaming button in the meeting toolbar. Streaming is OFF by default — you start it manually when the meeting begins.",
            })}
            checked={enabled}
            onChange={setEnabled}
          />

          {enabled && (
            <>
              <Field id="ls-url" label={t("createMeeting.livestreamUrl", { defaultValue: "RTMPS URL" })}>
                <Input
                  id="ls-url"
                  data-testid="ls-url"
                  type="url"
                  placeholder="rtmps://va.pscp.tv:443/x"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </Field>
              <Field id="ls-key" label={t("createMeeting.livestreamKey", { defaultValue: "Stream key" })}>
                <div className="flex gap-2">
                  <Input
                    id="ls-key"
                    data-testid="ls-key"
                    type={showKey ? "text" : "password"}
                    placeholder="abcd-1234-…"
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((s) => !s)}
                    className="px-2 text-xs text-slate-300 rounded-md bg-primary-800 hover:bg-primary-700 border border-primary-700"
                  >
                    {showKey
                      ? t("common.hide", { defaultValue: "Hide" })
                      : t("common.show", { defaultValue: "Show" })}
                  </button>
                </div>
              </Field>
              <div className="text-xs text-slate-400 leading-relaxed bg-primary-900/60 border border-primary-700 rounded-md p-3 space-y-1">
                <p className="font-medium text-slate-300">
                  {t("createMeeting.livestreamWhereTitle", {
                    defaultValue: "Where to find these on X (Twitter):",
                  })}
                </p>
                <ol className="list-decimal pl-4 space-y-0.5">
                  <li>
                    {t("createMeeting.livestreamStep1", {
                      defaultValue: "Open studio.x.com and sign in.",
                    })}
                  </li>
                  <li>
                    {t("createMeeting.livestreamStep2", {
                      defaultValue: "Click “Producer” in the left sidebar, then “Create broadcast”.",
                    })}
                  </li>
                  <li>
                    {t("createMeeting.livestreamStep3", {
                      defaultValue:
                        "Under “Source”, choose “External encoder”. X shows an RTMPS URL and a stream key — copy them into the two fields above.",
                    })}
                  </li>
                  <li>
                    {t("createMeeting.livestreamStep4", {
                      defaultValue:
                        "The key is single-use per broadcast: regenerate it on studio.x.com if you reuse this meeting later.",
                    })}
                  </li>
                </ol>
              </div>
            </>
          )}

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={busy} data-testid="ls-save">
              {busy
                ? t("common.saving", { defaultValue: "Saving…" })
                : t("common.save", { defaultValue: "Save" })}
            </Button>
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("common.cancel", { defaultValue: "Cancel" })}
            </Button>
          </div>

          {err && <div className="text-red-400 text-sm">{err}</div>}
        </form>
      </Card>
    </div>
  );
}
