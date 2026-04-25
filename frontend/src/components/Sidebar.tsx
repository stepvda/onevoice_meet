import { useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Home,
  LogIn,
  LogOut,
  Menu,
  Settings as SettingsIcon,
  Video,
  X,
  type LucideIcon,
} from "lucide-react";
import { isAuthenticated, logoutFromOneWitysk } from "../lib/auth";

interface NavItem {
  to: string;
  i18nKey: string;
  icon: LucideIcon;
  end?: boolean;
}

const primaryItems: NavItem[] = [
  { to: "/", i18nKey: "nav.home", icon: Home, end: true },
  { to: "/recordings", i18nKey: "nav.recordings", icon: Video },
];

const secondaryItems: NavItem[] = [
  { to: "/settings", i18nKey: "nav.settings", icon: SettingsIcon },
];

export default function Sidebar() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  // Hide sidebar entirely while the user is in a live meeting, and on the
  // headless egress recorder template so it doesn't appear in recordings.
  if (pathname.startsWith("/r/")) return null;

  const signedIn = isAuthenticated();

  async function handleLogout() {
    if (loggingOut) return;
    if (!confirm(t("nav.logoffConfirm"))) return;
    setLoggingOut(true);
    try {
      await logoutFromOneWitysk();
    } finally {
      setLoggingOut(false);
      // Navigate home and force a remount so the auth-gated UI rerenders
      // without the (now-cleared) cached token.
      navigate("/", { replace: true });
      window.location.reload();
    }
  }

  return (
    <>
      {/* Mobile top bar (above lg) */}
      <header className="lg:hidden fixed top-0 inset-x-0 z-30 flex items-center justify-between px-4 py-3 bg-primary-900/90 backdrop-blur border-b border-primary-700">
        <button
          type="button"
          onClick={() => setMobileOpen(true)}
          aria-label="Open navigation"
          className="p-2 rounded-lg hover:bg-primary-800 text-slate-100"
        >
          <Menu size={20} />
        </button>
        <span className="font-semibold text-slate-100">meet.witysk.org</span>
        <span className="w-9" />
      </header>

      {/* Sidebar (fixed on lg, drawer on mobile) */}
      <aside
        data-testid="sidebar"
        className={[
          "bg-witysk-sidebar text-white shadow-lg",
          "flex flex-col w-64 h-screen fixed top-0 left-0 z-40",
          "transition-transform lg:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0",
        ].join(" ")}
      >
        <div className="flex items-center justify-between px-6 pt-6 pb-4">
          <div>
            <div className="text-xl font-bold tracking-tight">meet.witysk.org</div>
            <div className="h-0.5 w-10 bg-accent-500 mt-1" />
          </div>
          <button
            type="button"
            className="lg:hidden p-1 rounded hover:bg-white/10"
            onClick={() => setMobileOpen(false)}
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
          {primaryItems.map(({ to, i18nKey, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) =>
                [
                  "flex items-center gap-3 px-4 py-3 rounded-lg transition-colors",
                  isActive
                    ? "bg-white/15 text-white font-semibold shadow-inner"
                    : "text-white/70 hover:bg-white/10 hover:text-white",
                ].join(" ")
              }
            >
              <Icon size={20} />
              <span>{t(i18nKey)}</span>
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-white/10 px-3 py-2 space-y-1">
          {secondaryItems.map(({ to, i18nKey, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) =>
                [
                  "flex items-center gap-3 px-4 py-3 rounded-lg transition-colors",
                  isActive
                    ? "bg-white/15 text-white font-semibold shadow-inner"
                    : "text-white/70 hover:bg-white/10 hover:text-white",
                ].join(" ")
              }
            >
              <Icon size={20} />
              <span>{t(i18nKey)}</span>
            </NavLink>
          ))}

          {signedIn ? (
            <button
              type="button"
              onClick={handleLogout}
              disabled={loggingOut}
              data-testid="sidebar-logout"
              title={t("nav.logoffTitle", { defaultValue: "Log off here and on one.witysk.org" })}
              className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-red-500 hover:bg-white/10 hover:text-red-400 transition-colors disabled:opacity-50"
            >
              <LogOut size={20} />
              <span>{loggingOut ? t("nav.loggingOff") : t("nav.logoff")}</span>
            </button>
          ) : (
            <a
              href="https://one.witysk.org"
              data-testid="sidebar-login"
              className="flex items-center gap-3 px-4 py-3 rounded-lg text-accent-500 hover:bg-white/10 hover:text-accent-400 transition-colors"
            >
              <LogIn size={20} />
              <span>{t("nav.signIn")}</span>
            </a>
          )}
        </div>
      </aside>

      {mobileOpen && (
        <div
          className="lg:hidden fixed inset-0 bg-black/50 z-30"
          onClick={() => setMobileOpen(false)}
          aria-hidden="true"
        />
      )}
    </>
  );
}

export function MainArea({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  const inMeeting = pathname.startsWith("/r/");
  return (
    <main
      className={
        inMeeting
          ? "h-screen w-screen"
          : "lg:pl-64 pt-14 lg:pt-0 min-h-screen"
      }
    >
      {children}
    </main>
  );
}
