import { lazy, Suspense, useEffect } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import Sidebar, { MainArea } from "./components/Sidebar";
// Route components are code-split so the initial bundle stays small. Without
// this, every visitor downloads the PayPal SDK, the LiveKit client, the emoji
// picker, and 18 routes' worth of JS just to see the home page. With it, only
// the routes the user actually visits get fetched.
const Lobby = lazy(() => import("./routes/Lobby"));
const Room = lazy(() => import("./routes/Room"));
const PublicView = lazy(() => import("./routes/PublicView"));
const EgressLayoutPiP = lazy(() => import("./routes/EgressLayoutPiP"));
const EgressLayoutComposite = lazy(() => import("./routes/EgressLayoutComposite"));
const CreateMeeting = lazy(() => import("./routes/CreateMeeting"));
const Recordings = lazy(() => import("./routes/Recordings"));
const Settings = lazy(() => import("./routes/Settings"));
const MeetingChat = lazy(() => import("./routes/MeetingChat"));
const TICafe = lazy(() => import("./routes/TICafe"));
const SsoCallback = lazy(() => import("./routes/SsoCallback"));
const SignUp = lazy(() => import("./routes/SignUp"));
const Login = lazy(() => import("./routes/Login"));
const Account = lazy(() => import("./routes/Account"));
const Terms = lazy(() => import("./routes/Terms"));
const Privacy = lazy(() => import("./routes/Privacy"));
const Legal = lazy(() => import("./routes/Legal"));
const Vouchers = lazy(() => import("./routes/Vouchers"));
const AdminPanel = lazy(() => import("./routes/AdminPanel"));
const Upgrade = lazy(() => import("./routes/Upgrade"));
const ForgotPassword = lazy(() => import("./routes/ForgotPassword"));
const ResetPassword = lazy(() => import("./routes/ResetPassword"));
import { bootstrapFromOneWitysk } from "./lib/auth";
import { syncServerLanguage } from "./i18n";
import { TICafeProvider } from "./lib/tiCafe";
import { usePrivacyServerSync, usePrivacyClassNames } from "./lib/privacy";
import { useThemePref } from "./lib/themePref";
import { useUiPrefs } from "./lib/uiPrefs";

export default function App() {
  // Side-effect hooks driven by user preferences. They live here so they're
  // active on every route (including pre-meeting pages), not just inside Room.
  useThemePref();
  useUiPrefs();
  usePrivacyServerSync();
  usePrivacyClassNames();
  // Once at app start: try to bootstrap an SSO token, then ask the API for the
  // user's saved language. If the user has explicitly chosen one in Settings
  // (`language_set_manually=true`), it overrides whatever the browser detected.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const tok = await bootstrapFromOneWitysk();
      if (cancelled || !tok) return;
      await syncServerLanguage();
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Egress-only routes render without the global Sidebar / MainArea
  // chrome — LiveKit's headless Chrome captures the page as-is, so any
  // app chrome (left-hand nav, top bar, etc.) would end up in the
  // recording / livestream output. Detected by path prefix so we don't
  // have to thread an `isEgress` prop through every layout component.
  const location = useLocation();
  const isEgressRoute = location.pathname.startsWith("/egress-layout/");
  if (isEgressRoute) {
    return (
      <Suspense fallback={<div style={{ background: "#000", position: "fixed", inset: 0 }} />}>
        <Routes>
          <Route path="/egress-layout/pip" element={<EgressLayoutPiP />} />
          <Route path="/egress-layout/composite" element={<EgressLayoutComposite />} />
        </Routes>
      </Suspense>
    );
  }

  // TICafeProvider wraps the entire app so the audio session survives route
  // changes — the only triggers that disconnect are an explicit click on the
  // bar's main toggle, or `window.dispatchEvent(new Event("ti-cafe-logout"))`
  // (fired from logoutFromOneWitysk()).
  return (
    <TICafeProvider>
      <Sidebar />
      <MainArea>
        {/* Suspense fallback shown while a lazy route's chunk is loading.
            Kept lightweight — a fancy spinner would defeat the purpose of
            shipping a small initial bundle. */}
        <Suspense fallback={<div className="p-6 text-slate-400 text-sm">…</div>}>
        <Routes>
          {/* Static paths first — React Router v6 ranks static above dynamic. */}
          <Route path="/" element={<CreateMeeting />} />
          <Route path="/recordings" element={<Recordings />} />
          <Route path="/ti-cafe" element={<TICafe />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/account" element={<Account />} />
          <Route path="/signup" element={<SignUp />} />
          <Route path="/login" element={<Login />} />
          <Route path="/terms" element={<Terms />} />
          <Route path="/privacy" element={<Privacy />} />
          <Route path="/legal" element={<Legal />} />
          <Route path="/vouchers" element={<Vouchers />} />
          <Route path="/admin" element={<AdminPanel />} />
          <Route path="/upgrade" element={<Upgrade />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/sso-callback" element={<SsoCallback />} />
          <Route path="/meetings/:meetingId/chat" element={<MeetingChat />} />
          {/* Public view-only stream. Anyone can open this URL — no auth,
              no publish rights, no participant-panel presence. */}
          <Route path="/public/:publicSlug" element={<PublicView />} />
          {/* Custom egress layout template — only loaded by LiveKit's
              headless Chrome when the meeting's `pip_enabled` flag is on.
              Reads url/token/room/layout query params (from LiveKit) plus
              our `overlay` param, connects to the room, and renders the
              PiP composition. */}
          <Route path="/egress-layout/pip" element={<EgressLayoutPiP />} />
          {/* Live meeting view */}
          <Route path="/r/:roomName" element={<Room />} />
          {/* Backward-compat: old links in the wild are /j/<slug> */}
          <Route path="/j/:roomName" element={<Lobby />} />
          {/* Clean shareable form: meet.witysk.org/<3-word-slug> → lobby. */}
          <Route path="/:roomName" element={<Lobby />} />
        </Routes>
        </Suspense>
      </MainArea>
    </TICafeProvider>
  );
}
