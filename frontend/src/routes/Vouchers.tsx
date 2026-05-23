import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Copy, Plus, Ticket, Trash2 } from "lucide-react";
import { api, VoucherOut } from "../lib/api";
import { bootstrapFromOneWitysk, isAuthenticated } from "../lib/auth";
import { Button, Card, Field, Input } from "../components/ui";
import SignInPrompt from "../components/SignInPrompt";

/**
 * Voucher administration. Backend already gates this to one.witysk.org
 * user_ids 1 (Stephane) and 404 (David); we just render the UI and show a
 * 403 fallback if the API refuses. No client-side gate is needed.
 */
const DURATIONS = [
  { days: 30, labelKey: "vouchers.dur1mo", defaultLabel: "1 month (30 days)" },
  { days: 60, labelKey: "vouchers.dur2mo", defaultLabel: "2 months (60 days)" },
  { days: 90, labelKey: "vouchers.dur3mo", defaultLabel: "3 months (90 days)" },
] as const;

export default function Vouchers() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<VoucherOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Issuance form
  const [duration, setDuration] = useState<number>(30);
  const [note, setNote] = useState("");
  const [issuing, setIssuing] = useState(false);
  const [justIssued, setJustIssued] = useState<VoucherOut | null>(null);

  async function load() {
    try {
      const r = await api.listVouchers();
      setRows(r);
      setForbidden(false);
    } catch (e) {
      const msg = (e as Error).message || "";
      if (/401|invalid token|expired/i.test(msg)) {
        setNeedsSignIn(true);
      } else if (/403|not authorised/i.test(msg)) {
        setForbidden(true);
      } else {
        setErr(msg);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isAuthenticated()) {
        const tok = await bootstrapFromOneWitysk();
        if (!tok) {
          if (!cancelled) {
            setNeedsSignIn(true);
            setLoading(false);
          }
          return;
        }
      }
      if (!cancelled) await load();
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function issue(e: React.FormEvent) {
    e.preventDefault();
    setIssuing(true);
    setErr(null);
    try {
      const v = await api.issueVoucher({ duration_days: duration, note: note.trim() || null });
      setJustIssued(v);
      setNote("");
      setRows((cur) => [v, ...cur]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setIssuing(false);
    }
  }

  async function revoke(v: VoucherOut) {
    // Stronger warning when the voucher was already redeemed — deletion
    // also revokes the redeemer's meeting-creation rights immediately.
    const msg = v.redeemed_at
      ? t("vouchers.revokeRedeemedConfirm", {
          code: v.code,
          defaultValue:
            "Voucher {{code}} has been redeemed. Deleting it will immediately revoke the redeemer's meeting-creation rights. Continue?",
        })
      : t("vouchers.revokeConfirm", { code: v.code, defaultValue: "Revoke voucher {{code}}?" });
    if (!confirm(msg)) return;
    try {
      await api.revokeVoucher(v.code);
      setRows((cur) => cur.filter((x) => x.code !== v.code));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function copy(code: string) {
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      window.prompt(t("vouchers.copyPrompt", { defaultValue: "Copy this code:" }), code);
    }
  }

  if (needsSignIn) {
    return (
      <div className="p-4 lg:p-8 max-w-4xl mx-auto">
        <SignInPrompt
          icon={Ticket}
          title={t("vouchers.signInTitle", { defaultValue: "Sign in to manage vouchers" })}
          body={t("vouchers.signInBody", { defaultValue: "Voucher administration requires an authorised one.witysk.org account." })}
          testId="vouchers-signin"
        />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div className="p-4 lg:p-8 max-w-4xl mx-auto" data-testid="vouchers-forbidden">
        <Card>
          <h1 className="text-xl font-bold text-slate-50 mb-2 flex items-center gap-2">
            <Ticket size={20} className="text-accent-500" />
            {t("vouchers.title", { defaultValue: "Vouchers" })}
          </h1>
          <p className="text-sm text-red-400">
            {t("vouchers.forbidden", { defaultValue: "You don't have permission to issue vouchers." })}
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="vouchers-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-4 flex items-center gap-2">
        <Ticket size={22} className="text-accent-500" />
        {t("vouchers.title", { defaultValue: "Vouchers" })}
      </h1>

      <Card className="mb-4">
        <h2 className="text-lg font-semibold text-slate-100 mb-3">
          {t("vouchers.issue", { defaultValue: "Issue a new voucher" })}
        </h2>
        <form onSubmit={issue} className="flex flex-col gap-3">
          <Field id="v-dur" label={t("vouchers.duration", { defaultValue: "Duration" })}>
            <select
              id="v-dur"
              aria-label={t("vouchers.duration", { defaultValue: "Duration" })}
              value={duration}
              onChange={(e) => setDuration(Number(e.target.value))}
              className="w-full px-3 py-2 rounded-lg bg-primary-900/60 text-slate-100 border border-primary-700"
            >
              {DURATIONS.map((d) => (
                <option key={d.days} value={d.days}>
                  {t(d.labelKey, { defaultValue: d.defaultLabel })}
                </option>
              ))}
            </select>
          </Field>
          <Field id="v-note" label={t("vouchers.note", { defaultValue: "Note (optional, internal only)" })}>
            <Input
              id="v-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={200}
              data-testid="voucher-note"
            />
          </Field>
          <div>
            <Button type="submit" disabled={issuing} data-testid="voucher-issue">
              <Plus size={16} />
              {issuing ? t("vouchers.issuing", { defaultValue: "Issuing…" }) : t("vouchers.issueSubmit", { defaultValue: "Issue voucher" })}
            </Button>
          </div>
          {err && <div className="text-sm text-red-400">{err}</div>}
        </form>

        {justIssued && (
          <div
            className="mt-4 p-3 rounded-lg bg-accent-500/10 border border-accent-500/40"
            data-testid="voucher-just-issued"
          >
            <div className="text-xs text-slate-400">
              {t("vouchers.justIssued", { defaultValue: "New voucher — copy and share it now:" })}
            </div>
            <div className="flex items-center gap-2 mt-1">
              <code className="text-2xl font-mono font-bold text-accent-500 tracking-widest">{justIssued.code}</code>
              <Button type="button" variant="ghost" size="sm" onClick={() => copy(justIssued.code)}>
                <Copy size={14} />
              </Button>
            </div>
            <div className="text-xs text-slate-500 mt-1">
              {t("vouchers.duration", { defaultValue: "Duration" })}: {justIssued.duration_days}{" "}
              {t("vouchers.days", { defaultValue: "days" })}
            </div>
          </div>
        )}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold text-slate-100 mb-3">
          {t("vouchers.list", { defaultValue: "Issued vouchers" })}
        </h2>
        {loading ? (
          <p className="text-slate-300">{t("vouchers.loading", { defaultValue: "Loading…" })}</p>
        ) : rows.length === 0 ? (
          <p className="text-slate-400 text-sm">
            {t("vouchers.empty", { defaultValue: "No vouchers issued yet." })}
          </p>
        ) : (
          <ul className="flex flex-col divide-y divide-primary-700">
            {rows.map((v) => (
              <li key={v.id} className="py-3 flex items-center gap-3" data-testid={`voucher-row-${v.code}`}>
                <code className="font-mono font-bold text-slate-100 tracking-wider">{v.code}</code>
                <span className="text-xs text-slate-500">
                  {v.duration_days} {t("vouchers.days", { defaultValue: "days" })}
                </span>
                {v.note && <span className="text-xs text-slate-400 italic truncate flex-1">{v.note}</span>}
                {!v.note && <span className="flex-1" />}
                {v.redeemed_at ? (
                  <>
                    <span className="text-xs text-accent-500">
                      {t("vouchers.redeemed", { defaultValue: "redeemed" })}{" "}
                      {new Date(v.redeemed_at).toLocaleDateString()}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => revoke(v)}
                      title={t("vouchers.revokeRedeemedTitle", {
                        defaultValue: "Delete voucher and revoke the redeemer's access",
                      })}
                    >
                      <Trash2 size={14} />
                    </Button>
                  </>
                ) : (
                  <>
                    {(() => {
                      const exp = new Date(v.expires_at);
                      const expired = exp.getTime() <= Date.now();
                      return (
                        <span className={expired ? "text-xs text-red-400" : "text-xs text-amber-400"}>
                          {expired
                            ? t("vouchers.expired", { defaultValue: "expired" })
                            : t("vouchers.unredeemed", { defaultValue: "unredeemed" })}
                          {" · "}
                          {t("vouchers.expiresOn", {
                            date: exp.toLocaleDateString(),
                            defaultValue: "expires {{date}}",
                          })}
                        </span>
                      );
                    })()}
                    <Button type="button" variant="ghost" size="sm" onClick={() => copy(v.code)}>
                      <Copy size={14} />
                    </Button>
                    <Button type="button" variant="ghost" size="sm" onClick={() => revoke(v)}>
                      <Trash2 size={14} />
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
