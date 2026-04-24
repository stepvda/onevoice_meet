import { Navigate, Route, Routes } from "react-router-dom";
import Sidebar, { MainArea } from "./components/Sidebar";
import Lobby from "./routes/Lobby";
import Room from "./routes/Room";
import CreateMeeting from "./routes/CreateMeeting";
import Recordings from "./routes/Recordings";
import Settings from "./routes/Settings";

export default function App() {
  return (
    <>
      <Sidebar />
      <MainArea>
        <Routes>
          <Route path="/" element={<CreateMeeting />} />
          <Route path="/j/:roomName" element={<Lobby />} />
          <Route path="/r/:roomName" element={<Room />} />
          <Route path="/recordings" element={<Recordings />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </MainArea>
    </>
  );
}
