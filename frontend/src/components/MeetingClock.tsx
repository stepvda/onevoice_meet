import { useEffect, useState } from "react";

export default function MeetingClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  return (
    <div
      data-testid="meeting-clock"
      className="absolute top-2 right-2 z-20 px-2 py-1 rounded-md bg-black/55 text-white text-xs font-mono pointer-events-none"
    >
      {hh}:{mm}
    </div>
  );
}
