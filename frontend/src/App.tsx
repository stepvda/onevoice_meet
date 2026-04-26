import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import Sidebar, { MainArea } from "./components/Sidebar";
import Lobby from "./routes/Lobby";
import Room from "./routes/Room";
import CreateMeeting from "./routes/CreateMeeting";
import Recordings from "./routes/Recordings";
import Settings from "./routes/Settings";
import MeetingChat from "./routes/MeetingChat";
import TICafe from "./routes/TICafe";
import SsoCallback from "./routes/SsoCallback";
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
        <Routes>
          {/* Static paths first — React Router v6 ranks static above dynamic. */}
          <Route path="/" element={<CreateMeeting />} />
          <Route path="/recordings" element={<Recordings />} />
          <Route path="/ti-cafe" element={<TICafe />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/sso-callback" element={<SsoCallback />} />
          <Route path="/meetings/:meetingId/chat" element={<MeetingChat />} />
          {/* Live meeting view */}
          <Route path="/r/:roomName" element={<Room />} />
          {/* Backward-compat: old links in the wild are /j/<slug> */}
          <Route path="/j/:roomName" element={<Lobby />} />
          {/* Clean shareable form: meet.witysk.org/<3-word-slug> → lobby. */}
          <Route path="/:roomName" element={<Lobby />} />
        </Routes>
      </MainArea>
    </TICafeProvider>
  );
}
