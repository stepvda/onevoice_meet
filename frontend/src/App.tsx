import { lazy, Suspense, useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import Sidebar, { MainArea } from "./components/Sidebar";
// Route components are code-split so the initial bundle stays small. Without
// this, every visitor downloads the PayPal SDK, the LiveKit client, the emoji
// picker, and 18 routes' worth of JS just to see the home page. With it, only
// the routes the user actually visits get fetched.
const Lobby = lazy(() => import("./routes/Lobby"));
const Room = lazy(() => import("./routes/Room"));
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

export default function App() {
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
