/**
 * Account-page subscription summary.
 *
 * Renders nothing for users with no entitlement (free tier or trial only)
 * and for SSO users (their access is governed by one.witysk.org). For
 * paid/voucher users it shows plan name + when they next renew (auto-renew
 * subs) or when access ends (one-shot orders + vouchers), plus a link to
 * /upgrade to manage or extend.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CreditCard } from "lucide-react";
import { api, BillingHistoryItem, MeOut } from "../lib/api";
import { Card } from "./ui";

interface Props {
  me: MeOut;
}

const AUTO_RENEW_KINDS = new Set(["paypal_monthly", "paypal_annual_sub"]);

export default function SubscriptionStatus({ me }: Props) {
  const { t } = useTranslation();
  const [history, setHistory] = useState<BillingHistoryItem[] | null>(null);
  const [historyErr, setHistoryErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (me.kind === "sso") return;
    api
      .myBillingHistory()
      .then((rows) => {
        if (!cancelled) setHistory(rows);
      })
      .catch((e: Error) => {
        if (!cancelled) setHistoryErr(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [me.kind, me.id]);

  function historyLabel(kind: BillingHistoryItem["kind"]): string {
    switch (kind) {
      case "paypal_order_monthly":
        return t("subStatus.histMonthlyOnce", { defaultValue: "Monthly pass (one-time)" });
      case "paypal_order_annual":
        return t("subStatus.histAnnualOnce", { defaultValue: "Yearly pass (one-time)" });
      case "paypal_subscription_monthly":
        return t("subStatus.histMonthlySub", { defaultValue: "Monthly subscription" });
      case "paypal_subscription_annual":
        return t("subStatus.histAnnualSub", { defaultValue: "Yearly subscription" });
      case "voucher":
        return t("subStatus.histVoucher", { defaultValue: "Voucher" });
    }
  }

  function planLabel(kind: string | null): string {
    switch (kind) {
      case "paypal_monthly":
        return t("subStatus.planMonthlySub", { defaultValue: "Monthly subscription (auto-renew)" });
      case "paypal_annual_sub":
        return t("subStatus.planAnnualSub", { defaultValue: "Yearly subscription (auto-renew)" });
      case "paypal_monthly_once":
        return t("subStatus.planMonthlyOnce", { defaultValue: "Monthly pass (one-time)" });
      case "paypal_annual":
        return t("subStatus.planAnnual", { defaultValue: "Yearly pass (one-time)" });
      case "voucher":
        return t("subStatus.planVoucher", { defaultValue: "Voucher" });
      default:
        return t("subStatus.planUnknown", { defaultValue: "Active plan" });
    }
  }
  // SSO users get admin from one.witysk.org; their entitlement_kind stays
  // null in our DB. Hide the section for them entirely.
  if (me.kind === "sso") return null;
  // For native users with no entitlement AND no past activity, hide the
  // card so the page doesn't show an empty section.
  const hasActive = me.entitlement_kind && me.entitlement_expires_at;
  const hasHistory = (history?.length ?? 0) > 0;
  if (!hasActive && !hasHistory && history !== null) return null;

  const expires = hasActive ? new Date(me.entitlement_expires_at as string) : null;
  const dateStr = expires?.toLocaleDateString() ?? "";
  const isAutoRenew = hasActive && AUTO_RENEW_KINDS.has(me.entitlement_kind as string);
  const now = Date.now();
  const msLeft = expires ? expires.getTime() - now : 0;
  const daysLeft = msLeft > 0 ? Math.floor(msLeft / 86_400_000) : 0;

  return (
    <Card data-testid="subscription-status">
      <h2 className="text-lg font-semibold text-slate-100 flex items-center gap-2 mb-1">
        <CreditCard size={18} className="text-accent-500" />
        {t("subStatus.title", { defaultValue: "Subscription" })}
      </h2>

      {hasActive ? (
        <>
          <p className="text-sm text-slate-300">
            <span className="font-medium">{planLabel(me.entitlement_kind)}</span>
          </p>
          <p className="text-sm text-slate-400 mt-1">
            {isAutoRenew
              ? t("subStatus.renewsOn", { date: dateStr, defaultValue: "Renews on {{date}}." })
              : t("subStatus.accessUntil", { date: dateStr, defaultValue: "Access until {{date}}." })}{" "}
            {daysLeft > 0 && (
              <span className="text-slate-500">
                {t("subStatus.daysLeft", { count: daysLeft, defaultValue: "{{count}} day(s) left." })}
              </span>
            )}
          </p>
          <div className="mt-3 flex gap-3 flex-wrap text-sm">
            <Link to="/upgrade" className="text-accent-500 hover:underline" data-testid="subscription-manage">
              {isAutoRenew
                ? t("subStatus.manage", { defaultValue: "Manage subscription" })
                : t("subStatus.extend", { defaultValue: "Extend or change plan" })}
            </Link>
          </div>
        </>
      ) : (
        <p className="text-sm text-slate-400">
          {t("subStatus.noActive", { defaultValue: "No active plan." })}{" "}
          <Link to="/upgrade" className="text-accent-500 hover:underline">
            {t("subStatus.viewPlans", { defaultValue: "View plans" })}
          </Link>
        </p>
      )}

      {/* Past activity. Always rendered when there is any, regardless of the
          current entitlement state, so the user can audit historical
          purchases / vouchers. */}
      {hasHistory && (
        <div className="mt-4 pt-3 border-t border-slate-700/40" data-testid="subscription-history">
          <h3 className="text-sm font-semibold text-slate-200 mb-2">
            {t("subStatus.historyTitle", { defaultValue: "History" })}
          </h3>
          <ul className="text-sm divide-y divide-slate-700/30">
            {history!.map((it, idx) => (
              <li key={`${it.reference ?? "x"}-${idx}`} className="py-2 flex items-baseline gap-3 flex-wrap">
                <span className="text-slate-300 min-w-[11rem] tabular-nums">
                  {new Date(it.date).toLocaleString(undefined, {
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
                <span className="text-slate-100 font-medium">{historyLabel(it.kind)}</span>
                {it.amount && (
                  <span className="text-slate-400">
                    {it.currency === "EUR" ? "€" : (it.currency ?? "")}
                    {it.amount}
                  </span>
                )}
                {it.kind === "voucher" && it.reference && (
                  <span className="font-mono text-xs text-slate-500">{it.reference}</span>
                )}
                {it.status && (
                  <span className="ml-auto text-xs text-slate-500 uppercase tracking-wide">
                    {it.status}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {historyErr && <p className="text-xs text-red-400 mt-2">{historyErr}</p>}
    </Card>
  );
}
