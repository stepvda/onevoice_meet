import { useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Coffee,
  CreditCard,
  Home,
  LogIn,
  LogOut,
  Menu,
  MonitorPlay,
  Settings as SettingsIcon,
  Shield,
  Ticket,
  User as UserIcon,
  UserPlus,
  Video,
  X,
  type LucideIcon,
} from "lucide-react";
import { isAuthenticated, logoutFromOneWitysk, startSsoRedirect } from "../lib/auth";
import { useMe } from "../lib/me";
import Footer from "./Footer";

interface NavItem {
  to: string;
  i18nKey: string;
  icon: LucideIcon;
  end?: boolean;
}

const primaryItems: NavItem[] = [
  { to: "/", i18nKey: "nav.home", icon: Home, end: true },
  { to: "/recordings", i18nKey: "nav.recordings", icon: Video },
  { to: "/on-demand", i18nKey: "nav.onDemand", icon: MonitorPlay },
  { to: "/ti-cafe", i18nKey: "nav.tiCafe", icon: Coffee },
];

// Items shown only when signed in. Settings stays for everyone (it gates
// itself); account / upgrade only matter once you have an identity to
// manage.
const authedSecondaryItems: NavItem[] = [
  { to: "/account", i18nKey: "nav.account", icon: UserIcon },
  { to: "/upgrade", i18nKey: "nav.upgrade", icon: CreditCard },
  { to: "/settings", i18nKey: "nav.settings", icon: SettingsIcon },
];

const anonSecondaryItems: NavItem[] = [
  { to: "/settings", i18nKey: "nav.settings", icon: SettingsIcon },
];

export default function Sidebar() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  // ALL hooks must run before any conditional return, otherwise React's
  // Rules of Hooks are violated when navigating between /r/<room> (early
  // return null) and other routes (full hook list). Putting the early
  // return below the useMe() call kept the order stable within each
  // render but unstable BETWEEN renders, which is what was causing the
  // SSO `/account` + `/upgrade` to leak through — useMe was returning
  // stale state.
  const signedIn = isAuthenticated();
  const { me } = useMe();
  // Hide sidebar entirely while the user is in a live meeting or watching
  // a public view-only stream — both are full-viewport surfaces with no
  // room for the nav rail.
  if (pathname.startsWith("/r/") || pathname.startsWith("/public/")) return null;

  // SSO accounts manage profile + facepic on one.witysk.org and always have
  // admin rights, so /account and /upgrade aren't useful — hide them. Native
  // users (and signed-out users) get the full secondary list.
  const isSso = me?.kind === "sso";
  const isVoucherAdmin = !!me?.is_voucher_admin;
  const isPlatformAdmin = !!me?.is_platform_admin;
  const baseSecondary = signedIn ? authedSecondaryItems : anonSecondaryItems;
  // Inject the /vouchers + /admin entries above /settings for privileged
  // accounts. Voucher admin gates ticket issuance; platform admin gates the
  // user-management / IP-block / IDS panel.
  const withExtras = (() => {
    if (!isVoucherAdmin && !isPlatformAdmin) return baseSecondary;
    const head = baseSecondary.filter((it) => it.to !== "/settings");
    const tail: NavItem[] = [];
    if (isVoucherAdmin) tail.push({ to: "/vouchers", i18nKey: "nav.vouchers", icon: Ticket });
    if (isPlatformAdmin) tail.push({ to: "/admin", i18nKey: "nav.admin", icon: Shield });
    tail.push({ to: "/settings", i18nKey: "nav.settings", icon: SettingsIcon });
    return [...head, ...tail];
  })();
  const secondaryForUser = withExtras.filter(
    (item) => !(isSso && (item.to === "/account" || item.to === "/upgrade"))
  );

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
          "flex flex-col w-64 h-dvh fixed top-0 left-0 z-40",
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

        {/* mb-[70px] reserves space for the global TICafeBar that overlays the
            footer; we add safe-area-bottom so the last menu item clears the
            iPhone home indicator. */}
        <div className="border-t border-white/10 px-3 py-2 space-y-1 mb-[calc(70px+env(safe-area-inset-bottom))]">
          {secondaryForUser.map(({ to, i18nKey, icon: Icon, end }) => (
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
            <>
              <button
                type="button"
                onClick={() => startSsoRedirect()}
                data-testid="sidebar-login-sso"
                title={t("nav.signInSsoTitle", { defaultValue: "Sign in with your one.witysk.org account" })}
                className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-accent-500 hover:bg-white/10 hover:text-accent-400 transition-colors"
              >
                <LogIn size={20} />
                <span>{t("nav.signInSso", { defaultValue: "Sign in with witysk.org" })}</span>
              </button>
              <NavLink
                to="/login"
                onClick={() => setMobileOpen(false)}
                data-testid="sidebar-login-native"
                className={({ isActive }) =>
                  [
                    "w-full flex items-center gap-3 px-4 py-3 rounded-lg transition-colors",
                    isActive
                      ? "bg-white/15 text-white font-semibold shadow-inner"
                      : "text-white/70 hover:bg-white/10 hover:text-white",
                  ].join(" ")
                }
              >
                <LogIn size={20} />
                <span>{t("nav.signIn", { defaultValue: "Sign in" })}</span>
              </NavLink>
              <NavLink
                to="/signup"
                onClick={() => setMobileOpen(false)}
                data-testid="sidebar-signup"
                className={({ isActive }) =>
                  [
                    "w-full flex items-center gap-3 px-4 py-3 rounded-lg transition-colors",
                    isActive
                      ? "bg-white/15 text-white font-semibold shadow-inner"
                      : "text-white/70 hover:bg-white/10 hover:text-white",
                  ].join(" ")
                }
              >
                <UserPlus size={20} />
                <span>{t("nav.signUp", { defaultValue: "Create account" })}</span>
              </NavLink>
            </>
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
  const fullScreen =
    pathname.startsWith("/r/") || pathname.startsWith("/public/");
  if (fullScreen) {
    return <main className="h-dvh w-screen">{children}</main>;
  }
  // flex-col so the footer sticks to the bottom even on short pages.
  // min-h-dvh tracks the iOS browser-chrome shrink/grow correctly; min-h-screen
  // (100vh) leaves a gap when the URL bar collapses.
  return (
    <main className="lg:pl-64 pt-14 lg:pt-0 min-h-dvh flex flex-col">
      <div className="flex-1">{children}</div>
      <Footer />
    </main>
  );
}
