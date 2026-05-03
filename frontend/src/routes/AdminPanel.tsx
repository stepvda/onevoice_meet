/**
 * Platform admin panel — three tabs: Users, IP blocks, IDS.
 *
 * Backend-gated: /api/v1/admin/* requires `is_platform_admin=true`. We also
 * gate client-side on `me.is_platform_admin` so non-admins see a "forbidden"
 * card instead of broken API calls.
 */
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Ban, Check, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, KeyRound, Plus, RefreshCw, Search, Shield, ShieldAlert, Trash2, Unlock, UserX } from "lucide-react";
import {
  AdminUserOut,
  BlockedIPOut,
  IdsEvent,
  IdsStatusOut,
  api,
} from "../lib/api";
import {
  bootstrapFromOneWitysk,
  fetchOneWityskUser,
  isAuthenticated,
  OneWityskUserDetail,
} from "../lib/auth";
import { useMe } from "../lib/me";
import { Button, Card, Field, Input, Modal } from "../components/ui";
import SignInPrompt from "../components/SignInPrompt";

type Tab = "users" | "ips" | "ids";

export default function AdminPanel() {
  const { t } = useTranslation();
  const { me, loading: meLoading } = useMe();
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [tab, setTab] = useState<Tab>("users");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isAuthenticated()) {
        const tok = await bootstrapFromOneWitysk();
        if (!tok) {
          if (!cancelled) {
            setNeedsSignIn(true);
            setBootstrapping(false);
          }
          return;
        }
      }
      if (!cancelled) setBootstrapping(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (needsSignIn) {
    return (
      <div className="p-4 lg:p-8 max-w-2xl mx-auto">
        <SignInPrompt
          icon={Shield}
          title={t("admin.signInTitle", { defaultValue: "Sign in to access the admin panel" })}
          body={t("admin.signInBody", { defaultValue: "The admin panel requires a platform-admin account." })}
          testId="admin-signin"
        />
      </div>
    );
  }

  if (bootstrapping || meLoading) {
    return (
      <div className="p-4 lg:p-8 max-w-3xl mx-auto">
        <Card>
          <p className="text-slate-300">{t("admin.loading", { defaultValue: "Loading…" })}</p>
        </Card>
      </div>
    );
  }

  if (!me?.is_platform_admin) {
    return (
      <div className="p-4 lg:p-8 max-w-2xl mx-auto" data-testid="admin-forbidden">
        <Card>
          <h1 className="text-xl font-bold text-slate-50 mb-2 flex items-center gap-2">
            <ShieldAlert size={20} className="text-red-400" />
            {t("admin.forbiddenTitle", { defaultValue: "Admin panel" })}
          </h1>
          <p className="text-sm text-red-400">
            {t("admin.forbidden", { defaultValue: "You don't have platform-admin rights." })}
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="p-4 lg:p-8 max-w-6xl mx-auto" data-testid="admin-panel">
      <h1 className="text-2xl font-bold text-slate-50 mb-4 flex items-center gap-2">
        <Shield size={22} className="text-accent-500" />
        {t("admin.title", { defaultValue: "Admin panel" })}
      </h1>

      <div className="flex gap-2 mb-4 border-b border-primary-700">
        <TabButton active={tab === "users"} onClick={() => setTab("users")}>
          {t("admin.tabs.users", { defaultValue: "Users" })}
        </TabButton>
        <TabButton active={tab === "ips"} onClick={() => setTab("ips")}>
          {t("admin.tabs.ips", { defaultValue: "IP blocks" })}
        </TabButton>
        <TabButton active={tab === "ids"} onClick={() => setTab("ids")}>
          {t("admin.tabs.ids", { defaultValue: "IDS" })}
        </TabButton>
      </div>

      {tab === "users" && <UsersTab currentUserId={me.id} />}
      {tab === "ips" && <BlockedIPsTab />}
      {tab === "ids" && <IdsTab />}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors",
        active
          ? "border-accent-500 text-accent-500"
          : "border-transparent text-slate-400 hover:text-slate-200",
      ].join(" ")}
    >
      {children}
    </button>
  );
}

// ─── Users tab ─────────────────────────────────────────────────────────

const PAGE_SIZE = 25;

type SortableColumn = "id" | "kind" | "email" | "username" | "name" | "created_at";
type SortOrder = "asc" | "desc";

function SortableTh({
  col,
  label,
  sortBy,
  sortOrder,
  onToggle,
}: {
  col: SortableColumn;
  label: string;
  sortBy: SortableColumn;
  sortOrder: SortOrder;
  onToggle: (c: SortableColumn) => void;
}) {
  const active = sortBy === col;
  return (
    <th className="py-2 pr-2 select-none">
      <button
        type="button"
        onClick={() => onToggle(col)}
        className={[
          "inline-flex items-center gap-1 uppercase tracking-wide",
          active ? "text-accent-500" : "text-slate-400 hover:text-slate-200",
        ].join(" ")}
        aria-label={`Sort by ${label}`}
      >
        <span>{label}</span>
        {active ? (
          sortOrder === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />
        ) : (
          <ArrowDown size={12} className="opacity-30" />
        )}
      </button>
    </th>
  );
}

function UsersTab({ currentUserId }: { currentUserId: number }) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<AdminUserOut[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [kindFilter, setKindFilter] = useState<"" | "sso" | "native">("");
  // 0-based page index. Search/filter changes reset it to 0 in `runQuery`.
  const [page, setPage] = useState(0);
  // Sort state. Changing sort always resets to page 0 — paging through a
  // mid-table after re-ordering would land on bizarre rows.
  const [sortBy, setSortBy] = useState<SortableColumn>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");

  const lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  async function load(pageToLoad: number, sb: SortableColumn, so: SortOrder) {
    setLoading(true);
    setErr(null);
    try {
      const r = await api.adminListUsers({
        q: q.trim() || undefined,
        kind: kindFilter || undefined,
        limit: PAGE_SIZE,
        offset: pageToLoad * PAGE_SIZE,
        sort_by: sb,
        sort_order: so,
      });
      setRows(r.users);
      setTotal(r.total);
      // If a deletion or filter change pushed us past the last page, snap back.
      const newLast = Math.max(0, Math.ceil(r.total / PAGE_SIZE) - 1);
      if (pageToLoad > newLast) {
        setPage(newLast);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  // Search / filter / refresh always reset to page 0.
  function runQuery() {
    setPage(0);
    void load(0, sortBy, sortOrder);
  }

  // Column-header click: same column flips order, different column starts asc.
  function toggleSort(col: SortableColumn) {
    let nextOrder: SortOrder;
    if (col === sortBy) {
      nextOrder = sortOrder === "asc" ? "desc" : "asc";
      setSortOrder(nextOrder);
    } else {
      nextOrder = "asc";
      setSortBy(col);
      setSortOrder(nextOrder);
    }
    setPage(0);
    void load(0, col, nextOrder);
  }

  useEffect(() => {
    void load(page, sortBy, sortOrder);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  // SSO enrichment — keyed by external_id. We store one of:
  //   undefined  → not yet fetched
  //   null       → fetched, returned 401/403/404 (caller is not a one.witysk
  //                admin, or user no longer exists)
  //   detail     → fetched OK
  // Cached across page changes, so paging back to a previously-viewed page
  // doesn't re-hammer one.witysk.org.
  const [ssoDetails, setSsoDetails] = useState<Record<string, OneWityskUserDetail | null>>({});

  useEffect(() => {
    const todo = rows
      .filter((u) => u.kind === "sso" && u.external_id && !(u.external_id in ssoDetails))
      .map((u) => u.external_id as string);
    if (todo.length === 0) return;
    let cancelled = false;
    (async () => {
      const fetched = await Promise.all(
        todo.map(async (extId) => [extId, await fetchOneWityskUser(extId)] as const)
      );
      if (cancelled) return;
      setSsoDetails((prev) => {
        const next = { ...prev };
        for (const [k, v] of fetched) next[k] = v;
        return next;
      });
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);

  function ssoFor(u: AdminUserOut): OneWityskUserDetail | null | undefined {
    if (u.kind !== "sso" || !u.external_id) return null;
    return ssoDetails[u.external_id];
  }

  // Modal-based admin actions. Replaces native window.confirm/prompt/alert
  // which look broken on mobile (tiny text, keyboard covers the input). One
  // discriminated state describes the open dialog and what action to run on
  // confirm; renderDialog() below maps it to the right form.
  type Dialog =
    | { kind: "togglePlatformAdmin"; user: AdminUserOut }
    | { kind: "suspend"; user: AdminUserOut; reason: string }
    | { kind: "setPassword"; user: AdminUserOut; pw: string; valError: string | null }
    | { kind: "passwordSet"; user: AdminUserOut }
    | { kind: "delete"; user: AdminUserOut };
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [dialogErr, setDialogErr] = useState<string | null>(null);

  function openDialog(d: Dialog) {
    setDialog(d);
    setDialogErr(null);
  }
  function closeDialog() {
    if (dialogBusy) return;
    setDialog(null);
    setDialogErr(null);
  }

  // Toggle suspend has two paths: unsuspend is direct (no info needed), suspend
  // opens the reason prompt.
  async function toggleDisabled(u: AdminUserOut) {
    if (u.is_disabled) {
      try {
        const updated = await api.adminUpdateUser(u.id, { is_disabled: false });
        setRows((cur) => cur.map((x) => (x.id === u.id ? updated : x)));
      } catch (e) {
        setErr((e as Error).message);
      }
      return;
    }
    openDialog({ kind: "suspend", user: u, reason: "" });
  }

  async function runDialog() {
    if (!dialog) return;
    setDialogBusy(true);
    setDialogErr(null);
    try {
      if (dialog.kind === "togglePlatformAdmin") {
        const u = dialog.user;
        const updated = await api.adminUpdateUser(u.id, { is_platform_admin: !u.is_platform_admin });
        setRows((cur) => cur.map((x) => (x.id === u.id ? updated : x)));
        setDialog(null);
      } else if (dialog.kind === "suspend") {
        const u = dialog.user;
        const updated = await api.adminUpdateUser(u.id, {
          is_disabled: true,
          disable_reason: dialog.reason.trim() || null,
        });
        setRows((cur) => cur.map((x) => (x.id === u.id ? updated : x)));
        setDialog(null);
      } else if (dialog.kind === "setPassword") {
        if (dialog.pw.length < 8) {
          setDialog({ ...dialog, valError: t("admin.users.passwordTooShort", { defaultValue: "Password must be at least 8 characters." }) });
          return;
        }
        await api.adminSetPassword(dialog.user.id, dialog.pw);
        setDialog({ kind: "passwordSet", user: dialog.user });
      } else if (dialog.kind === "delete") {
        await api.adminDeleteUser(dialog.user.id);
        setRows((cur) => cur.filter((x) => x.id !== dialog.user.id));
        setTotal((n) => Math.max(0, n - 1));
        setDialog(null);
      } else if (dialog.kind === "passwordSet") {
        setDialog(null);
      }
    } catch (e) {
      setDialogErr((e as Error).message);
    } finally {
      setDialogBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3 mb-4">
        <Field id="users-q" label={t("admin.users.search", { defaultValue: "Search" })}>
          <div className="relative">
            <Search size={14} className="absolute left-2 top-2.5 text-slate-500" />
            <Input
              id="users-q"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") runQuery();
              }}
              placeholder={t("admin.users.searchPlaceholder", { defaultValue: "email, username, name" })}
              className="pl-7"
            />
          </div>
        </Field>
        <Field id="users-kind" label={t("admin.users.kind", { defaultValue: "Kind" })}>
          <select
            id="users-kind"
            aria-label={t("admin.users.kind", { defaultValue: "Kind" })}
            value={kindFilter}
            onChange={(e) => {
              setKindFilter(e.target.value as "" | "sso" | "native");
              // changing the filter should reset paging
              setPage(0);
            }}
            className="px-3 py-2 rounded-lg bg-primary-900/60 text-slate-100 border border-primary-700"
          >
            <option value="">{t("admin.users.kindAny", { defaultValue: "Any" })}</option>
            <option value="sso">SSO</option>
            <option value="native">Native</option>
          </select>
        </Field>
        <Button type="button" onClick={runQuery} variant="secondary">
          <RefreshCw size={14} />
          {t("admin.users.refresh", { defaultValue: "Refresh" })}
        </Button>
        <span className="text-xs text-slate-400 ml-auto">
          {t("admin.users.totalCount", { count: total, defaultValue: "{{count}} users" })}
        </span>
      </div>

      {err && <div className="text-sm text-red-400 mb-3">{err}</div>}

      {loading ? (
        <p className="text-slate-300">{t("admin.loading", { defaultValue: "Loading…" })}</p>
      ) : rows.length === 0 ? (
        <p className="text-slate-400 text-sm">{t("admin.users.empty", { defaultValue: "No users matched." })}</p>
      ) : (
        <>
        {/* Card layout for narrow screens — the 10-column table is unusable
            on a phone. Same data, restacked. Hidden on md+. */}
        <ul className="md:hidden flex flex-col gap-3" data-testid="admin-users-cards">
          {rows.map((u) => {
            const sso = ssoFor(u);
            const ssoLoading = u.kind === "sso" && sso === undefined;
            const name = u.name ?? sso?.name ?? null;
            const email = u.email ?? sso?.email ?? null;
            const username = u.username ?? sso?.username ?? null;
            const location =
              sso?.city || sso?.country
                ? [sso?.city, sso?.country].filter(Boolean).join(", ")
                : null;
            const lastLogin = sso?.last_activity ?? null;
            const oneWityskAdmin = sso?.is_admin ?? false;
            return (
              <li
                key={u.id}
                data-testid={`admin-user-${u.id}`}
                className={[
                  "rounded-lg border border-primary-700 bg-primary-900/40 p-3",
                  u.is_disabled ? "opacity-60" : "",
                ].join(" ")}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-slate-100 font-medium truncate">
                      {name ?? username ?? email ?? `#${u.id}`}
                    </div>
                    <div className="text-xs text-slate-400 truncate">
                      {email ?? username ?? "—"}
                    </div>
                  </div>
                  <span className="text-xs text-slate-500 font-mono">#{u.id}</span>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-slate-400">
                  <div><span className="text-slate-500">{t("admin.users.colKind", { defaultValue: "Kind" })}:</span> {u.kind}</div>
                  <div className="truncate"><span className="text-slate-500">{t("admin.users.colLocation", { defaultValue: "Location" })}:</span> {location ?? (ssoLoading ? "…" : "—")}</div>
                  <div><span className="text-slate-500">{t("admin.users.colCreated", { defaultValue: "Created" })}:</span> {u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}</div>
                  <div><span className="text-slate-500">{t("admin.users.colLastLogin", { defaultValue: "Last login" })}:</span> {lastLogin ? new Date(lastLogin).toLocaleDateString() : ssoLoading ? "…" : "—"}</div>
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {u.is_platform_admin && (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-accent-500/15 text-accent-400 text-xs">
                      <Shield size={10} />
                      {t("admin.users.badgePlatformAdmin", { defaultValue: "admin" })}
                    </span>
                  )}
                  {oneWityskAdmin && (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-700 text-slate-200 text-xs">
                      one.witysk
                    </span>
                  )}
                  {u.is_admin && (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-700 text-slate-200 text-xs">
                      meet
                    </span>
                  )}
                  {u.is_disabled && (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-red-500/15 text-red-400 text-xs">
                      <Ban size={10} />
                      {t("admin.users.badgeSuspended", { defaultValue: "suspended" })}
                    </span>
                  )}
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-1 -mb-1">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => openDialog({ kind: "togglePlatformAdmin", user: u })}
                    disabled={u.id === currentUserId && u.is_platform_admin}
                    title={u.is_platform_admin
                      ? t("admin.users.demote", { defaultValue: "Revoke platform-admin" })
                      : t("admin.users.promote", { defaultValue: "Grant platform-admin" })}
                  >
                    {u.is_platform_admin ? <ShieldAlert size={14} /> : <Shield size={14} />}
                  </Button>
                  {u.kind === "native" && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => openDialog({ kind: "setPassword", user: u, pw: "", valError: null })}
                      title={t("admin.users.setPassword", { defaultValue: "Set new password" })}
                    >
                      <KeyRound size={14} />
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void toggleDisabled(u)}
                    disabled={u.id === currentUserId}
                    title={u.is_disabled
                      ? t("admin.users.unsuspend", { defaultValue: "Unsuspend" })
                      : t("admin.users.suspend", { defaultValue: "Suspend" })}
                  >
                    {u.is_disabled ? <Check size={14} /> : <UserX size={14} />}
                  </Button>
                  {u.kind === "native" && u.id !== currentUserId && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => openDialog({ kind: "delete", user: u })}
                      title={t("admin.users.delete", { defaultValue: "Delete account" })}
                    >
                      <Trash2 size={14} />
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>

        <div className="hidden md:block overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-400 border-b border-primary-700">
              <tr>
                <SortableTh col="id" label={t("admin.users.colId", { defaultValue: "ID" })} sortBy={sortBy} sortOrder={sortOrder} onToggle={toggleSort} />
                <SortableTh col="kind" label={t("admin.users.colKind", { defaultValue: "Kind" })} sortBy={sortBy} sortOrder={sortOrder} onToggle={toggleSort} />
                <SortableTh col="name" label={t("admin.users.colName", { defaultValue: "Name" })} sortBy={sortBy} sortOrder={sortOrder} onToggle={toggleSort} />
                <SortableTh col="email" label={t("admin.users.colEmail", { defaultValue: "Email" })} sortBy={sortBy} sortOrder={sortOrder} onToggle={toggleSort} />
                <SortableTh col="username" label={t("admin.users.colUsername", { defaultValue: "Username" })} sortBy={sortBy} sortOrder={sortOrder} onToggle={toggleSort} />
                <th
                  className="py-2 pr-2"
                  title={t("admin.users.notSortable", { defaultValue: "Not sortable — value comes from one.witysk.org per row" })}
                >
                  {t("admin.users.colLocation", { defaultValue: "Location" })}
                </th>
                <SortableTh col="created_at" label={t("admin.users.colCreated", { defaultValue: "Created" })} sortBy={sortBy} sortOrder={sortOrder} onToggle={toggleSort} />
                <th
                  className="py-2 pr-2"
                  title={t("admin.users.notSortable", { defaultValue: "Not sortable — value comes from one.witysk.org per row" })}
                >
                  {t("admin.users.colLastLogin", { defaultValue: "Last login" })}
                </th>
                <th className="py-2 pr-2">{t("admin.users.colStatus", { defaultValue: "Status" })}</th>
                <th className="py-2 pr-2 text-right">{t("admin.users.colActions", { defaultValue: "Actions" })}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-primary-700">
              {rows.map((u) => {
                const sso = ssoFor(u);
                const ssoLoading = u.kind === "sso" && sso === undefined;
                const name = u.name ?? sso?.name ?? null;
                const email = u.email ?? sso?.email ?? null;
                const username = u.username ?? sso?.username ?? null;
                const location =
                  sso?.city || sso?.country
                    ? [sso?.city, sso?.country].filter(Boolean).join(", ")
                    : null;
                const created = u.created_at;
                const lastLogin = sso?.last_activity ?? null;
                const oneWityskAdmin = sso?.is_admin ?? false;
                return (
                <tr key={u.id} data-testid={`admin-user-${u.id}`} className={u.is_disabled ? "opacity-60" : ""}>
                  <td className="py-2 pr-2 text-slate-400 font-mono">{u.id}</td>
                  <td className="py-2 pr-2 text-slate-300">{u.kind}</td>
                  <td className="py-2 pr-2 text-slate-100">
                    {name ?? (ssoLoading ? <span className="text-slate-500">…</span> : <span className="text-slate-500">—</span>)}
                  </td>
                  <td className="py-2 pr-2 text-slate-100">
                    {email ?? (ssoLoading ? <span className="text-slate-500">…</span> : <span className="text-slate-500">—</span>)}
                  </td>
                  <td className="py-2 pr-2 text-slate-300">
                    {username ?? (ssoLoading ? <span className="text-slate-500">…</span> : <span className="text-slate-500">—</span>)}
                  </td>
                  <td className="py-2 pr-2 text-slate-300">
                    {location ?? (ssoLoading ? <span className="text-slate-500">…</span> : <span className="text-slate-500">—</span>)}
                  </td>
                  <td className="py-2 pr-2 text-slate-400 whitespace-nowrap">
                    {created ? new Date(created).toLocaleDateString() : <span className="text-slate-500">—</span>}
                  </td>
                  <td className="py-2 pr-2 text-slate-400 whitespace-nowrap">
                    {lastLogin
                      ? new Date(lastLogin).toLocaleDateString()
                      : ssoLoading
                      ? <span className="text-slate-500">…</span>
                      : <span className="text-slate-500">—</span>}
                  </td>
                  <td className="py-2 pr-2 space-x-1">
                    {u.is_platform_admin && (
                      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-accent-500/15 text-accent-400 text-xs">
                        <Shield size={10} />
                        {t("admin.users.badgePlatformAdmin", { defaultValue: "admin" })}
                      </span>
                    )}
                    {oneWityskAdmin && (
                      <span
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-700 text-slate-200 text-xs"
                        title={t("admin.users.badgeOneWityskAdminTitle", {
                          defaultValue: "Admin on one.witysk.org",
                        })}
                      >
                        one.witysk
                      </span>
                    )}
                    {u.is_admin && (
                      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-700 text-slate-200 text-xs">
                        meet
                      </span>
                    )}
                    {u.is_disabled && (
                      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-red-500/15 text-red-400 text-xs">
                        <Ban size={10} />
                        {t("admin.users.badgeSuspended", { defaultValue: "suspended" })}
                      </span>
                    )}
                  </td>
                  <td className="py-2 pl-2 text-right whitespace-nowrap">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => openDialog({ kind: "togglePlatformAdmin", user: u })}
                      disabled={u.id === currentUserId && u.is_platform_admin}
                      title={
                        u.is_platform_admin
                          ? t("admin.users.demote", { defaultValue: "Revoke platform-admin" })
                          : t("admin.users.promote", { defaultValue: "Grant platform-admin" })
                      }
                    >
                      {u.is_platform_admin ? <ShieldAlert size={14} /> : <Shield size={14} />}
                    </Button>
                    {u.kind === "native" && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => openDialog({ kind: "setPassword", user: u, pw: "", valError: null })}
                        title={t("admin.users.setPassword", { defaultValue: "Set new password" })}
                      >
                        <KeyRound size={14} />
                      </Button>
                    )}
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => void toggleDisabled(u)}
                      disabled={u.id === currentUserId}
                      title={
                        u.is_disabled
                          ? t("admin.users.unsuspend", { defaultValue: "Unsuspend" })
                          : t("admin.users.suspend", { defaultValue: "Suspend" })
                      }
                    >
                      {u.is_disabled ? <Check size={14} /> : <UserX size={14} />}
                    </Button>
                    {u.kind === "native" && u.id !== currentUserId && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => openDialog({ kind: "delete", user: u })}
                        title={t("admin.users.delete", { defaultValue: "Delete account" })}
                      >
                        <Trash2 size={14} />
                      </Button>
                    )}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        </>
      )}

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between mt-4 pt-3 border-t border-primary-700">
          <span className="text-xs text-slate-400">
            {t("admin.users.pageStatus", {
              page: page + 1,
              pages: lastPage + 1,
              defaultValue: "Page {{page}} of {{pages}}",
            })}
          </span>
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPage(0)}
              disabled={page === 0 || loading}
              title={t("admin.users.firstPage", { defaultValue: "First page" })}
            >
              <ChevronsLeft size={14} />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0 || loading}
              title={t("admin.users.prevPage", { defaultValue: "Previous page" })}
            >
              <ChevronLeft size={14} />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPage((p) => Math.min(lastPage, p + 1))}
              disabled={page >= lastPage || loading}
              title={t("admin.users.nextPage", { defaultValue: "Next page" })}
            >
              <ChevronRight size={14} />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPage(lastPage)}
              disabled={page >= lastPage || loading}
              title={t("admin.users.lastPage", { defaultValue: "Last page" })}
            >
              <ChevronsRight size={14} />
            </Button>
          </div>
        </div>
      )}

      {/* Admin action dialogs. Replaces native window.prompt/confirm/alert
          which look broken on phones. The discriminator on `dialog.kind`
          decides which form to render; runDialog() executes on confirm. */}
      <Modal
        open={!!dialog}
        onClose={closeDialog}
        title={
          dialog?.kind === "togglePlatformAdmin"
            ? dialog.user.is_platform_admin
              ? t("admin.users.demote", { defaultValue: "Revoke platform-admin" })
              : t("admin.users.promote", { defaultValue: "Grant platform-admin" })
            : dialog?.kind === "suspend"
            ? t("admin.users.suspend", { defaultValue: "Suspend" })
            : dialog?.kind === "setPassword"
            ? t("admin.users.setPassword", { defaultValue: "Set new password" })
            : dialog?.kind === "passwordSet"
            ? t("admin.users.passwordSet", { defaultValue: "Password updated." })
            : dialog?.kind === "delete"
            ? t("admin.users.delete", { defaultValue: "Delete account" })
            : ""
        }
        footer={
          <>
            {dialog?.kind !== "passwordSet" && (
              <Button type="button" variant="secondary" onClick={closeDialog} disabled={dialogBusy}>
                {t("admin.users.cancel", { defaultValue: "Cancel" })}
              </Button>
            )}
            <Button
              type="button"
              variant={dialog?.kind === "delete" ? "danger" : "primary"}
              onClick={() => void runDialog()}
              disabled={dialogBusy}
            >
              {dialogBusy
                ? t("admin.users.working", { defaultValue: "Working…" })
                : dialog?.kind === "passwordSet"
                ? t("admin.users.ok", { defaultValue: "OK" })
                : t("admin.users.confirm", { defaultValue: "Confirm" })}
            </Button>
          </>
        }
      >
        {dialog?.kind === "togglePlatformAdmin" && (
          <p>
            {dialog.user.is_platform_admin
              ? t("admin.users.confirmDemote", {
                  email: dialog.user.email || dialog.user.username,
                  defaultValue: "Revoke platform-admin from {{email}}?",
                })
              : t("admin.users.confirmPromote", {
                  email: dialog.user.email || dialog.user.username,
                  defaultValue: "Grant platform-admin to {{email}}?",
                })}
          </p>
        )}
        {dialog?.kind === "suspend" && (
          <Field id="dlg-suspend-reason" label={t("admin.users.suspendReasonPrompt", {
            defaultValue: "Reason for suspending this account (optional, shown to admins):",
          })}>
            <Input
              id="dlg-suspend-reason"
              value={dialog.reason}
              onChange={(e) => setDialog({ ...dialog, reason: e.target.value })}
              maxLength={255}
              autoFocus
            />
          </Field>
        )}
        {dialog?.kind === "setPassword" && (
          <Field id="dlg-set-password" label={t("admin.users.newPasswordPrompt", {
            email: dialog.user.email || dialog.user.username,
            defaultValue: "New password for {{email}} (8+ chars):",
          })}>
            <Input
              id="dlg-set-password"
              type="password"
              value={dialog.pw}
              onChange={(e) => setDialog({ ...dialog, pw: e.target.value, valError: null })}
              maxLength={200}
              autoFocus
            />
            {dialog.valError && <div className="text-xs text-red-400 mt-1">{dialog.valError}</div>}
          </Field>
        )}
        {dialog?.kind === "passwordSet" && (
          <p>{t("admin.users.passwordSet", { defaultValue: "Password updated." })}</p>
        )}
        {dialog?.kind === "delete" && (
          <p>
            {t("admin.users.confirmDelete", {
              email: dialog.user.email || dialog.user.username,
              defaultValue: "Delete account {{email}}? This cannot be undone.",
            })}
          </p>
        )}
        {dialogErr && <div className="text-sm text-red-400 mt-3">{dialogErr}</div>}
      </Modal>
    </Card>
  );
}

// ─── Blocked IPs tab ───────────────────────────────────────────────────

function BlockedIPsTab() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<BlockedIPOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [ip, setIp] = useState("");
  const [reason, setReason] = useState("");
  const [adding, setAdding] = useState(false);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      setRows(await api.adminListBlockedIps());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setAdding(true);
    setErr(null);
    try {
      await api.adminAddBlockedIp({ ip_address: ip.trim(), reason: reason.trim() || null });
      setIp("");
      setReason("");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setAdding(false);
    }
  }

  async function toggle(row: BlockedIPOut) {
    try {
      const updated = await api.adminUpdateBlockedIp(row.id, { is_enabled: !row.is_enabled });
      setRows((cur) => cur.map((x) => (x.id === row.id ? updated : x)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function remove(row: BlockedIPOut) {
    if (!confirm(t("admin.ips.confirmDelete", { ip: row.ip_address, defaultValue: "Delete block for {{ip}}?" }))) return;
    try {
      await api.adminDeleteBlockedIp(row.id);
      setRows((cur) => cur.filter((x) => x.id !== row.id));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <h2 className="text-lg font-semibold text-slate-100 mb-3">
          {t("admin.ips.addTitle", { defaultValue: "Block an IP, CIDR, or range" })}
        </h2>
        <form onSubmit={add} className="grid grid-cols-1 md:grid-cols-[1fr_2fr_auto] gap-3 items-end">
          <Field id="block-ip" label={t("admin.ips.ipLabel", { defaultValue: "Address" })}
            hint={t("admin.ips.ipHint", { defaultValue: "Examples: 203.0.113.5, 203.0.113.0/24, 203.0.113.5-50, 2001:db8::/32" })}>
            <Input
              id="block-ip"
              value={ip}
              onChange={(e) => setIp(e.target.value)}
              placeholder="203.0.113.5"
              data-testid="block-ip"
            />
          </Field>
          <Field id="block-reason" label={t("admin.ips.reasonLabel", { defaultValue: "Reason (optional)" })}>
            <Input
              id="block-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={255}
              data-testid="block-reason"
            />
          </Field>
          <Button type="submit" disabled={adding || !ip.trim()} data-testid="block-add">
            <Plus size={16} />
            {adding ? t("admin.ips.adding", { defaultValue: "Adding…" }) : t("admin.ips.add", { defaultValue: "Block" })}
          </Button>
        </form>
        {err && <div className="text-sm text-red-400 mt-2">{err}</div>}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold text-slate-100 mb-3">
          {t("admin.ips.listTitle", { defaultValue: "Blocked addresses" })}
        </h2>
        {loading ? (
          <p className="text-slate-300">{t("admin.loading", { defaultValue: "Loading…" })}</p>
        ) : rows.length === 0 ? (
          <p className="text-slate-400 text-sm">{t("admin.ips.empty", { defaultValue: "No IP blocks configured." })}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-400 border-b border-primary-700">
                <tr>
                  <th className="py-2 pr-2">{t("admin.ips.colIp", { defaultValue: "Address" })}</th>
                  <th className="py-2 pr-2">{t("admin.ips.colReason", { defaultValue: "Reason" })}</th>
                  <th className="py-2 pr-2 text-right">{t("admin.ips.colHits", { defaultValue: "Hits" })}</th>
                  <th className="py-2 pr-2">{t("admin.ips.colState", { defaultValue: "State" })}</th>
                  <th className="py-2 pr-2 text-right">{t("admin.users.colActions", { defaultValue: "Actions" })}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-primary-700">
                {rows.map((row) => (
                  <tr key={row.id} className={row.is_enabled ? "" : "opacity-60"}>
                    <td className="py-2 pr-2 font-mono text-slate-100">{row.ip_address}</td>
                    <td className="py-2 pr-2 text-slate-300">
                      {row.reason || <span className="text-slate-500">—</span>}
                    </td>
                    <td className="py-2 pr-2 text-right text-slate-300">
                      {row.block_count}
                      {row.live_hits > 0 && (
                        <span className="text-amber-400 text-xs ml-1">+{row.live_hits}</span>
                      )}
                    </td>
                    <td className="py-2 pr-2">
                      {row.is_enabled ? (
                        <span className="text-red-400 text-xs">{t("admin.ips.stateActive", { defaultValue: "active" })}</span>
                      ) : (
                        <span className="text-slate-500 text-xs">{t("admin.ips.stateDisabled", { defaultValue: "disabled" })}</span>
                      )}
                    </td>
                    <td className="py-2 pl-2 text-right whitespace-nowrap">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => void toggle(row)}
                        title={
                          row.is_enabled
                            ? t("admin.ips.disable", { defaultValue: "Disable (keeps row)" })
                            : t("admin.ips.enable", { defaultValue: "Re-enable" })
                        }
                      >
                        {row.is_enabled ? <Ban size={14} /> : <Check size={14} />}
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => void remove(row)}
                        title={t("admin.ips.delete", { defaultValue: "Remove" })}
                      >
                        <Trash2 size={14} />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

// ─── IDS tab ──────────────────────────────────────────────────────────

function IdsTab() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<IdsStatusOut | null>(null);
  const [events, setEvents] = useState<IdsEvent[]>([]);
  const [persisted, setPersisted] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const [s, ev] = await Promise.all([
        api.adminIdsStatus(),
        api.adminIdsEvents({ limit: 100, persisted }),
      ]);
      setStatus(s);
      setEvents(ev);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setLoading(true);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persisted]);

  async function unblock(ip: string) {
    if (!confirm(t("admin.ids.confirmUnblock", { ip, defaultValue: "Unblock {{ip}}?" }))) return;
    try {
      await api.adminIdsUnblock(ip);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const severityColor = useMemo<Record<string, string>>(
    () => ({
      info: "text-slate-400",
      warn: "text-amber-400",
      block: "text-red-400",
      alert: "text-red-500 font-semibold",
    }),
    []
  );

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-slate-100">
            {t("admin.ids.statusTitle", { defaultValue: "IDS status" })}
          </h2>
          <Button type="button" variant="secondary" size="sm" onClick={() => void load()}>
            <RefreshCw size={14} />
            {t("admin.users.refresh", { defaultValue: "Refresh" })}
          </Button>
        </div>
        {err && <div className="text-sm text-red-400 mb-3">{err}</div>}
        {loading ? (
          <p className="text-slate-300">{t("admin.loading", { defaultValue: "Loading…" })}</p>
        ) : status ? (
          <>
            <dl className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <Stat label={t("admin.ids.statEnabled", { defaultValue: "Enabled" })} value={status.enabled ? "yes" : "no"} />
              <Stat label={t("admin.ids.statTracked", { defaultValue: "Tracked IPs" })} value={String(status.tracked_ips)} />
              <Stat label={t("admin.ids.statTempBlocks", { defaultValue: "Temp-blocked" })} value={String(status.temp_blocked)} />
              <Stat label={t("admin.ids.statEvents", { defaultValue: "Events in memory" })} value={String(status.events_in_memory)} />
            </dl>
            <h3 className="text-sm font-semibold text-slate-200 mb-2">
              {t("admin.ids.activeBlocksTitle", { defaultValue: "Active temp blocks" })}
            </h3>
            {status.temp_blocks.length === 0 ? (
              <p className="text-slate-400 text-sm">
                {t("admin.ids.noTempBlocks", { defaultValue: "No temp-blocked IPs right now." })}
              </p>
            ) : (
              <ul className="divide-y divide-primary-700">
                {status.temp_blocks.map((b) => (
                  <li key={b.ip} className="py-2 flex items-center gap-3">
                    <code className="font-mono text-slate-100">{b.ip}</code>
                    <span className="text-xs text-slate-400">
                      {Math.floor(b.seconds_remaining / 60)}m {b.seconds_remaining % 60}s
                    </span>
                    <span className="text-xs text-slate-500">
                      {t("admin.ids.hits", { count: b.hits, defaultValue: "{{count}} hits" })}
                    </span>
                    <span className="flex-1" />
                    <Button type="button" variant="ghost" size="sm" onClick={() => void unblock(b.ip)}>
                      <Unlock size={14} />
                      {t("admin.ids.unblock", { defaultValue: "Unblock" })}
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}
      </Card>

      <Card>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-slate-100">
            {t("admin.ids.eventsTitle", { defaultValue: "Recent events" })}
          </h2>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={persisted}
              onChange={(e) => setPersisted(e.target.checked)}
            />
            {t("admin.ids.persistedToggle", { defaultValue: "From DB (history)" })}
          </label>
        </div>
        {events.length === 0 ? (
          <p className="text-slate-400 text-sm">{t("admin.ids.noEvents", { defaultValue: "No events yet." })}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left uppercase tracking-wide text-slate-400 border-b border-primary-700">
                <tr>
                  <th className="py-2 pr-2">{t("admin.ids.colTime", { defaultValue: "Time" })}</th>
                  <th className="py-2 pr-2">{t("admin.ids.colSeverity", { defaultValue: "Sev" })}</th>
                  <th className="py-2 pr-2">{t("admin.ids.colType", { defaultValue: "Type" })}</th>
                  <th className="py-2 pr-2">{t("admin.ids.colIp", { defaultValue: "IP" })}</th>
                  <th className="py-2 pr-2">{t("admin.ids.colHandle", { defaultValue: "Handle" })}</th>
                  <th className="py-2 pr-2">{t("admin.ids.colPath", { defaultValue: "Path" })}</th>
                  <th className="py-2 pr-2">{t("admin.ids.colDetails", { defaultValue: "Details" })}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-primary-700">
                {events.map((ev, i) => (
                  <tr key={i}>
                    <td className="py-1.5 pr-2 text-slate-400 whitespace-nowrap">
                      {ev.ts ? new Date(ev.ts).toLocaleTimeString() : "—"}
                    </td>
                    <td className={`py-1.5 pr-2 ${severityColor[ev.severity] || "text-slate-400"}`}>{ev.severity}</td>
                    <td className="py-1.5 pr-2 text-slate-200">{ev.event_type}</td>
                    <td className="py-1.5 pr-2 font-mono text-slate-300">{ev.ip || "—"}</td>
                    <td className="py-1.5 pr-2 text-slate-300">{ev.handle || "—"}</td>
                    <td className="py-1.5 pr-2 text-slate-400">{ev.path || "—"}</td>
                    <td className="py-1.5 pr-2 text-slate-500 truncate max-w-xs" title={ev.details || ""}>
                      {ev.details || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-primary-900/50 border border-primary-700 rounded-lg px-3 py-2">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="text-lg font-semibold text-slate-100">{value}</div>
    </div>
  );
}
