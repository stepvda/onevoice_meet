import { NavLink, useLocation } from "react-router-dom";
import { isAuthenticated } from "../lib/auth";

export default function NavBar() {
  const { pathname } = useLocation();

  // Hide the nav bar when the user is actually in a meeting so the video UI
  // gets the full viewport.
  if (pathname.startsWith("/r/")) return null;

  const signedIn = isAuthenticated();

  return (
    <nav
      data-testid="nav"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "1rem",
        padding: "0.75rem 1.25rem",
        background: "#0f0f14",
        borderBottom: "1px solid #232330",
      }}
    >
      <strong style={{ marginRight: "1rem" }}>meet.witysk.org</strong>
      <NavLink to="/" end style={navStyle}>
        Home
      </NavLink>
      <NavLink to="/recordings" style={navStyle}>
        Recordings
      </NavLink>
      <NavLink to="/settings" style={navStyle}>
        Settings
      </NavLink>
      <div style={{ flex: 1 }} />
      <small style={{ color: signedIn ? "#5bd16d" : "#aaa" }}>
        {signedIn ? "signed in via one.witysk.org" : "not signed in"}
      </small>
    </nav>
  );
}

const navStyle = ({ isActive }: { isActive: boolean }): React.CSSProperties => ({
  color: isActive ? "#72b7ff" : "#eaeaea",
  textDecoration: "none",
  padding: "0.3rem 0.6rem",
  borderRadius: 4,
  background: isActive ? "#1a2130" : "transparent",
});
