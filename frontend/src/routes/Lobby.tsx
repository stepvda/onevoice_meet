import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, AnonTokenResponse, PublicRoomInfo } from "../lib/api";
import { bootstrapFromOneWitysk, fetchOneWityskName, getAccessToken, isAuthenticated } from "../lib/auth";
import { Button, Card, Field, Input } from "../components/ui";

const CACHE_KEY = "meet:pending-token";

export function loadPendingToken(): AnonTokenResponse | null {
  const raw = sessionStorage.getItem(CACHE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function clearPendingToken(): void {
  sessionStorage.removeItem(CACHE_KEY);
}

const META_KEY = "meet:room-meta";

export function loadRoomMeta(): { display_title?: string; branding_url?: string | null } {
  const raw = sessionStorage.getItem(META_KEY);
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

export function clearRoomMeta(): void {
  sessionStorage.removeItem(META_KEY);
}

export default function Lobby() {
  const { t } = useTranslation();
  const { roomName = "" } = useParams();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<PublicRoomInfo | null>(null);
  // Pre-flight mic/camera state — null=untested, "ok"=granted, "denied"=blocked,
  // "error"=other (no devices, secure-context, etc.). We surface the result
  // before joining the room so a denied prompt doesn't dump the user mid-call.
  const [permState, setPermState] = useState<null | "ok" | "denied" | "error">(null);
  const [permTesting, setPermTesting] = useState(false);

  const [ownerMeetingId, setOwnerMeetingId] = useState<string | null>(
    sessionStorage.getItem(`owner:${roomName}`)
  );
  const isOwner = !!ownerMeetingId && isAuthenticated();

  // Public room metadata (title + branding) — works for both owner and anon.
  useEffect(() => {
    if (!roomName) return;
    let cancelled = false;
    api
      .publicRoomInfo(roomName)
      .then((i) => {
        if (cancelled) return;
        setInfo(i);
        sessionStorage.setItem(
          META_KEY,
          JSON.stringify({ display_title: i.display_title, branding_url: i.branding_url })
        );
      })
      .catch(() => {
        /* lobby still renders without meta */
      });
    return () => {
      cancelled = true;
    };
  }, [roomName]);

  // Pre-fill the display-name input from one.witysk.org when the visitor is
  // signed in but isn't the meeting owner (owners skip the form entirely).
  // Bootstrap SSO if no token is cached yet, then read /api/auth/me. We only
  // overwrite an empty `name` so we never clobber what the user typed.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const tok = getAccessToken() ?? (await bootstrapFromOneWitysk());
      if (!tok || cancelled) return;
      const fetched = await fetchOneWityskName();
      if (cancelled || !fetched) return;
      setName((cur) => (cur === "" ? fetched : cur));
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // If we're authenticated but the sessionStorage flag isn't set, ask the API
  // whether this user owns the meeting and re-establish the flag.
  useEffect(() => {
    if (ownerMeetingId) return;
    let cancelled = false;
    (async () => {
      const tok = isAuthenticated() ? localStorage.getItem("access_token") : await bootstrapFromOneWitysk();
      if (!tok) return;
      try {
        const all = await api.listMeetings();
        const mine = all.find((m) => m.room_name === roomName);
        if (mine && !cancelled) {
          sessionStorage.setItem(`owner:${roomName}`, mine.id);
          setOwnerMeetingId(mine.id);
        }
      } catch {
        /* not authorised, or API down — fall through to anon flow */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ownerMeetingId, roomName]);

  async function checkMediaPermission() {
    if (!navigator.mediaDevices?.getUserMedia) {
      setPermState("error");
      return;
    }
    setPermTesting(true);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
      // Stop tracks immediately — we just wanted the prompt, LiveKit will
      // re-acquire on join.
      stream.getTracks().forEach((t) => t.stop());
      setPermState("ok");
    } catch (e) {
      const msg = (e as DOMException)?.name ?? "";
      setPermState(msg === "NotAllowedError" || msg === "PermissionDeniedError" ? "denied" : "error");
    } finally {
      setPermTesting(false);
    }
  }

  async function join(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const resp = isOwner
        ? await api.ownerToken(ownerMeetingId!, { display_name: await fetchOneWityskName() })
        : await api.anonToken(roomName, {
            display_name: name,
            email: email || undefined,
            password: password || undefined,
          });
      sessionStorage.setItem(CACHE_KEY, JSON.stringify(resp));
      navigate(`/r/${roomName}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-xl mx-auto">
      <Card>
        <div className="flex items-start gap-4 mb-4">
          {info?.branding_url && (
            <img
              src={info.branding_url}
              alt={t("lobby.brandingAlt")}
              data-testid="lobby-branding"
              className="h-16 w-16 object-cover rounded-md border border-primary-700 flex-shrink-0"
            />
          )}
          <div className="min-w-0">
            <h1 className="text-2xl font-bold text-slate-50 truncate">
              {info?.display_title || t("lobby.joinMeeting")}
            </h1>
            <p className="text-sm text-slate-400">
              {t("lobby.room")} <code className="text-slate-200">{roomName}</code>
              {isOwner && <span className="ml-2 text-accent-500">{t("lobby.host")}</span>}
            </p>
            {info?.owner_name && (
              <p className="text-sm text-slate-400 mt-0.5" data-testid="lobby-host">
                {t("lobby.hostedBy", { name: info.owner_name, defaultValue: "Hosted by {{name}}" })}
              </p>
            )}
          </div>
        </div>

        <form onSubmit={join} className="flex flex-col gap-4">
          {!isOwner && (
            <>
              <Field id="lobby-name" label={t("lobby.yourName")}>
                <Input
                  id="lobby-name"
                  data-testid="lobby-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  maxLength={80}
                />
              </Field>
              <Field id="lobby-email" label={t("lobby.emailOptional")}>
                <Input
                  id="lobby-email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </Field>
              <Field id="lobby-password" label={t("lobby.passwordIfRequired")}>
                <Input
                  id="lobby-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Field>
            </>
          )}

          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" disabled={busy || (!isOwner && !name)} data-testid="lobby-submit">
                {busy ? t("lobby.joining") : t("lobby.join")}
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => void checkMediaPermission()}
                disabled={permTesting}
                data-testid="lobby-test-media"
                title={t("lobby.testMediaTitle", { defaultValue: "Test microphone and camera before joining" })}
              >
                {permTesting
                  ? t("lobby.testingMedia", { defaultValue: "Testing…" })
                  : t("lobby.testMedia", { defaultValue: "Test mic & camera" })}
              </Button>
            </div>
            {permState === "ok" && (
              <div className="text-xs text-accent-500" data-testid="lobby-perm-ok">
                {t("lobby.mediaOk", { defaultValue: "Mic & camera ready." })}
              </div>
            )}
            {permState === "denied" && (
              <div className="text-xs text-red-400" data-testid="lobby-perm-denied">
                {t("lobby.mediaDenied", {
                  defaultValue:
                    "Mic or camera blocked. Tap the lock icon in the address bar to allow access, then reload.",
                })}
              </div>
            )}
            {permState === "error" && (
              <div className="text-xs text-amber-400" data-testid="lobby-perm-error">
                {t("lobby.mediaError", {
                  defaultValue: "Couldn't access mic/camera — check that no other app is using them.",
                })}
              </div>
            )}
            {err && <div className="text-red-400 text-sm">{err}</div>}
          </div>
        </form>
      </Card>
    </div>
  );
}
