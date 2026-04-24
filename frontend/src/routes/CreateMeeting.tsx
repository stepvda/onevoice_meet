import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Video } from "lucide-react";
import { api } from "../lib/api";
import { bootstrapFromOneWitysk, isAuthenticated } from "../lib/auth";
import { usePreferences } from "../lib/preferences";
import { Button, Card, Field, Input, Toggle } from "../components/ui";
import MyMeetings from "../components/MyMeetings";

type AuthState = "bootstrapping" | "authenticated" | "anonymous";

export default function CreateMeeting() {
  const navigate = useNavigate();
  const prefs = usePreferences((s) => s.meetingDefaults);
  const [title, setTitle] = useState("");
  const [password, setPassword] = useState("");
  const [usePassword, setUsePassword] = useState(prefs.requirePassword);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [authState, setAuthState] = useState<AuthState>(
    isAuthenticated() ? "authenticated" : "bootstrapping"
  );

  useEffect(() => {
    if (authState !== "bootstrapping") return;
    let cancelled = false;
    bootstrapFromOneWitysk().then((token) => {
      if (cancelled) return;
      setAuthState(token ? "authenticated" : "anonymous");
    });
    return () => {
      cancelled = true;
    };
  }, [authState]);

  if (authState === "bootstrapping") {
    return (
      <div className="p-4 lg:p-8 max-w-xl mx-auto">
        <Card>
          <h1 className="text-2xl font-bold">meet.witysk.org</h1>
          <p className="text-slate-300 mt-2">Checking your one.witysk.org session…</p>
        </Card>
      </div>
    );
  }

  if (authState === "anonymous") {
    return (
      <div className="p-4 lg:p-8 max-w-xl mx-auto">
        <Card>
          <h1 className="text-2xl font-bold">meet.witysk.org</h1>
          <p className="mt-2 text-slate-200">
            You need to sign in on{" "}
            <a className="text-primary-200 underline" href="https://one.witysk.org">
              one.witysk.org
            </a>{" "}
            before you can create meetings.
          </p>
          <p className="mt-2 text-slate-400">
            If you already have a join link, open it directly — no account required to join.
          </p>
        </Card>
      </div>
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = await api.createMeeting({
        display_title: title,
        password: usePassword && password ? password : undefined,
      });
      sessionStorage.setItem(`owner:${res.meeting.room_name}`, res.meeting.id);
      navigate(`/j/${res.meeting.room_name}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-2xl mx-auto flex flex-col gap-6">
      <MyMeetings refreshKey={busy ? 0 : 1} />
      <Card>
        <div className="flex items-center gap-3 mb-4">
          <div className="p-2 rounded-lg bg-accent-500/20 text-accent-500">
            <Video size={22} />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-slate-50">Create a meeting</h1>
            <p className="text-sm text-slate-400">
              Using your defaults: up to {prefs.maxParticipants} participants,{" "}
              {prefs.recordingMode === "off"
                ? "no recording"
                : prefs.recordingMode === "auto_on_start"
                ? "auto-record on start"
                : "manual recording"}
              .{" "}
              <a className="text-primary-200 underline" href="/settings">
                Change defaults
              </a>
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <Field id="meeting-title" label="Title">
            <Input
              id="meeting-title"
              data-testid="meeting-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              maxLength={200}
              placeholder="Weekly sync, 1:1 with Alice, etc."
            />
          </Field>

          <Toggle
            id="meeting-use-password"
            label="Require a password"
            checked={usePassword}
            onChange={setUsePassword}
          />

          {usePassword && (
            <Field id="meeting-password" label="Password">
              <Input
                id="meeting-password"
                data-testid="meeting-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required={usePassword}
              />
            </Field>
          )}

          <div>
            <Button type="submit" disabled={busy || !title} data-testid="create-submit">
              {busy ? "Creating…" : "Create meeting"}
            </Button>
            {err && <div className="text-red-400 text-sm mt-2">{err}</div>}
          </div>
        </form>
      </Card>
    </div>
  );
}
