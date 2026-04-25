import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ImagePlus, Video, X } from "lucide-react";
import { api } from "../lib/api";
import { bootstrapFromOneWitysk, isAuthenticated } from "../lib/auth";
import { usePreferences } from "../lib/preferences";
import { Button, Card, Field, Input, Label, Toggle } from "../components/ui";
import MyMeetings from "../components/MyMeetings";
import DiscoverableMeetings from "../components/DiscoverableMeetings";

const MAX_BRANDING_BYTES = 2 * 1024 * 1024;
const ALLOWED_BRANDING_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

type AuthState = "bootstrapping" | "authenticated" | "anonymous";

export default function CreateMeeting() {
  const navigate = useNavigate();
  const prefs = usePreferences((s) => s.meetingDefaults);
  const [title, setTitle] = useState("");
  const [password, setPassword] = useState("");
  const [usePassword, setUsePassword] = useState(prefs.requirePassword);
  const [listForAuth, setListForAuth] = useState(false);
  const [listForAnon, setListForAnon] = useState(false);
  const [branding, setBranding] = useState<File | null>(null);
  const [brandingPreview, setBrandingPreview] = useState<string | null>(null);
  const brandingInputRef = useRef<HTMLInputElement | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function pickBranding(file: File | null) {
    if (!file) {
      setBranding(null);
      if (brandingPreview) URL.revokeObjectURL(brandingPreview);
      setBrandingPreview(null);
      return;
    }
    if (!ALLOWED_BRANDING_TYPES.includes(file.type)) {
      setErr(`Unsupported image type ${file.type}; use JPEG, PNG, WebP, or GIF.`);
      return;
    }
    if (file.size > MAX_BRANDING_BYTES) {
      setErr(`Image is ${(file.size / 1_048_576).toFixed(1)} MB; max is 2 MB.`);
      return;
    }
    setErr(null);
    setBranding(file);
    if (brandingPreview) URL.revokeObjectURL(brandingPreview);
    setBrandingPreview(URL.createObjectURL(file));
  }
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
      <div className="p-4 lg:p-8 max-w-xl mx-auto flex flex-col gap-6">
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
        <DiscoverableMeetings />
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
        // Anonymous discovery implies authenticated.
        list_for_authenticated: listForAuth || listForAnon,
        list_for_anonymous: listForAnon,
      });
      // Upload branding (if chosen) before navigation. Non-fatal if it fails.
      if (branding) {
        try {
          await api.uploadBranding(res.meeting.id, branding);
        } catch (e) {
          setErr(`Meeting created but branding upload failed: ${(e as Error).message}`);
        }
      }
      sessionStorage.setItem(`owner:${res.meeting.room_name}`, res.meeting.id);
      navigate(`/${res.meeting.room_name}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-2xl mx-auto flex flex-col gap-6">
      <MyMeetings refreshKey={busy ? 0 : 1} />
      <DiscoverableMeetings />
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

          <div className="space-y-2 border-t border-primary-700 pt-3">
            <p className="text-sm text-slate-300 font-medium">Visibility</p>
            <p className="text-xs text-slate-400">
              By default, only you see this meeting on your Home page.
            </p>
            <Toggle
              id="meeting-list-auth"
              label="List on the Home page of other signed-in users"
              description="Anyone signed in via one.witysk.org can find and join."
              checked={listForAuth || listForAnon}
              onChange={(v) => {
                setListForAuth(v);
                if (!v) setListForAnon(false);
              }}
            />
            <Toggle
              id="meeting-list-anon"
              label="Also list to anonymous (non-signed-in) visitors"
              description="The meeting appears on the public landing page."
              checked={listForAnon}
              onChange={(v) => {
                setListForAnon(v);
                if (v) setListForAuth(true);
              }}
            />
          </div>

          <div>
            <Label htmlFor="meeting-branding">Branding image (optional)</Label>
            <p className="text-xs text-slate-400 mb-2">
              Shown in the lobby and the meeting top bar. JPEG, PNG, WebP or GIF; up to 2 MB.
            </p>
            <input
              ref={brandingInputRef}
              id="meeting-branding"
              data-testid="meeting-branding"
              type="file"
              aria-label="Branding image"
              title="Branding image"
              accept={ALLOWED_BRANDING_TYPES.join(",")}
              onChange={(e) => pickBranding(e.target.files?.[0] ?? null)}
              className="block text-sm text-slate-300 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-primary-700 file:text-slate-100 hover:file:bg-primary-600"
            />
            {brandingPreview && (
              <div className="mt-3 flex items-center gap-3">
                <img
                  src={brandingPreview}
                  alt="Branding preview"
                  data-testid="meeting-branding-preview"
                  className="h-16 w-16 object-cover rounded-md border border-primary-700"
                />
                <button
                  type="button"
                  onClick={() => {
                    pickBranding(null);
                    if (brandingInputRef.current) brandingInputRef.current.value = "";
                  }}
                  className="inline-flex items-center gap-1 text-sm text-slate-300 hover:text-slate-100"
                >
                  <X size={14} /> Remove
                </button>
              </div>
            )}
            {!brandingPreview && (
              <span className="inline-flex items-center gap-1 text-xs text-slate-500 mt-2">
                <ImagePlus size={14} /> No image selected
              </span>
            )}
          </div>

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
