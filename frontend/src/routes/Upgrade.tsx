import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Calendar, CalendarDays, CreditCard, Sparkles, Ticket } from "lucide-react";
import { api, MeOut } from "../lib/api";
import { bootstrapFromOneWitysk, isAuthenticated } from "../lib/auth";
import { refreshMe } from "../lib/me";
import { Button, Card, Field, Input } from "../components/ui";
import SignInPrompt from "../components/SignInPrompt";
import PaypalButtons from "../components/PaypalButtons";

/**
 * Upgrade / billing page. Three paths to admin rights for native users:
 *   - Redeem a voucher code (live)
 *   - Subscribe €2 / month via PayPal (Phase 3 — UI placeholder until
 *     PayPal Buttons SDK is wired)
 *   - Buy €20 / year via PayPal (same)
 */
export default function Upgrade() {
  const { t } = useTranslation();
  const [me, setMe] = useState<MeOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [code, setCode] = useState("");
  const [redeeming, setRedeeming] = useState(false);
  const [redeemErr, setRedeemErr] = useState<string | null>(null);
  const [redeemOk, setRedeemOk] = useState<{ days: number; expiresAt: string } | null>(null);
  const [startingTrial, setStartingTrial] = useState(false);
  const [trialErr, setTrialErr] = useState<string | null>(null);
  const [billing, setBilling] = useState<{
    enabled: boolean;
    client_id: string;
    plan_id_monthly: string;
    plan_id_annual: string;
    monthly_price: string;
    monthly_currency: string;
    annual_price: string;
    annual_currency: string;
  } | null>(null);
  const [billingMsg, setBillingMsg] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const subscriptionActive = me?.entitlement_kind === "paypal_monthly" || me?.entitlement_kind === "paypal_annual_sub";

  async function cancelSubscription() {
    if (!confirm(t("upgrade.cancelConfirm", {
      defaultValue: "Cancel auto-renewal? You keep access until the period you've already paid for ends.",
    }))) return;
    setCancelling(true);
    setBillingMsg(null);
    try {
      await api.cancelPaypalSubscription();
      await refreshMe();
      const u = await api.me();
      setMe(u);
      setBillingMsg(t("upgrade.cancelOk", {
        defaultValue: "Auto-renewal cancelled. Access continues until your paid period ends.",
      }));
    } catch (e) {
      setBillingMsg((e as Error).message);
    } finally {
      setCancelling(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    api
      .billingConfig()
      .then((c) => {
        if (!cancelled) setBilling(c);
      })
      .catch(() => {
        /* config endpoint should always work; if not, hide buttons */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function startTrial() {
    setStartingTrial(true);
    setTrialErr(null);
    try {
      const u = await api.startTrial();
      setMe(u);
      await refreshMe();
    } catch (e) {
      setTrialErr((e as Error).message);
    } finally {
      setStartingTrial(false);
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
      try {
        const u = await api.me();
        if (!cancelled) setMe(u);
      } catch {
        if (!cancelled) setNeedsSignIn(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function redeem(e: React.FormEvent) {
    e.preventDefault();
    setRedeeming(true);
    setRedeemErr(null);
    setRedeemOk(null);
    try {
      const r = await api.redeemVoucher(code.trim());
      setRedeemOk({ days: r.duration_days, expiresAt: r.entitlement_expires_at });
      setCode("");
      // refresh me
      const u = await api.me();
      setMe(u);
    } catch (e) {
      setRedeemErr((e as Error).message);
    } finally {
      setRedeeming(false);
    }
  }

  if (needsSignIn) {
    return (
      <div className="p-4 lg:p-8 max-w-4xl mx-auto">
        <SignInPrompt
          icon={CreditCard}
          title={t("upgrade.signInTitle", { defaultValue: "Sign in to upgrade" })}
          body={t("upgrade.signInBody", { defaultValue: "Sign in (or create an account) to redeem a voucher or subscribe." })}
          testId="upgrade-signin"
        />
      </div>
    );
  }

  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="upgrade-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-2 flex items-center gap-2">
        <CreditCard size={22} className="text-accent-500" />
        {t("upgrade.title", { defaultValue: "Upgrade" })}
      </h1>
      <p className="text-sm text-slate-400 mb-4">
        {t("upgrade.intro", {
          defaultValue:
            "Get unlimited meeting creation. Pick monthly or yearly billing, or redeem a voucher.",
        })}
      </p>

      {loading ? (
        <Card><p className="text-slate-300">{t("upgrade.loading", { defaultValue: "Loading…" })}</p></Card>
      ) : (
        <>
        {/* Free trial — visible to native users only. Three states:
            (a) trial_used=false → Start trial button.
            (b) trial active → "X days remaining" badge.
            (c) trial used + expired → small notice, no button. */}
        {me && me.kind === "native" && (
          <Card className="mb-4 border-accent-500/30" data-testid="upgrade-trial-card">
            <h2 className="text-lg font-semibold text-slate-100 flex items-center gap-2">
              <Sparkles size={18} className="text-accent-500" />
              {t("upgrade.trialTitle", { defaultValue: "10-day free trial" })}
            </h2>
            {!me.trial_used ? (
              <>
                <p className="text-sm text-slate-400 mt-2">
                  {t("upgrade.trialOffer", {
                    defaultValue:
                      "Try meet with full meeting-creation rights for 10 days. One-time per account — no card required.",
                  })}
                </p>
                <div className="mt-3">
                  <Button type="button" onClick={startTrial} disabled={startingTrial} data-testid="upgrade-start-trial">
                    <Sparkles size={16} />
                    {startingTrial
                      ? t("upgrade.startingTrial", { defaultValue: "Starting…" })
                      : t("upgrade.startTrial", { defaultValue: "Start free trial" })}
                  </Button>
                </div>
              </>
            ) : me.trial_days_remaining != null ? (
              <p className="text-sm text-amber-400 mt-2" data-testid="upgrade-trial-active">
                {t("upgrade.trialActive", {
                  count: me.trial_days_remaining,
                  defaultValue: "Trial active — {{count}} day(s) left.",
                })}
              </p>
            ) : (
              <p className="text-sm text-slate-500 mt-2" data-testid="upgrade-trial-expired">
                {t("upgrade.trialExpired", {
                  defaultValue: "Your trial has been used. Subscribe or redeem a voucher to keep creating meetings.",
                })}
              </p>
            )}
            {trialErr && <p className="text-sm text-red-400 mt-2">{trialErr}</p>}
          </Card>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          {/* Monthly — subscribe (auto-renew) + pay-once */}
          <Card>
            <h2 className="text-lg font-semibold text-slate-100 flex items-center gap-2">
              <Calendar size={18} className="text-accent-500" />
              {t("upgrade.monthlyTitle", { defaultValue: "Monthly" })}
            </h2>
            <p className="text-3xl font-bold text-slate-50 mt-2">
              €{billing?.monthly_price ?? "2"}
              <span className="text-sm font-normal text-slate-400"> / {t("upgrade.month", { defaultValue: "month" })}</span>
            </p>
            <ul className="text-sm text-slate-400 mt-3 space-y-1 list-disc list-inside">
              <li>{t("upgrade.unlimitedMeetings", { defaultValue: "Unlimited meetings" })}</li>
              <li>{t("upgrade.choosePath", { defaultValue: "Auto-renew or one-time billing" })}</li>
              <li>{t("upgrade.cancelAnytime", { defaultValue: "Cancel anytime in PayPal" })}</li>
            </ul>
            {!billing ? (
              <p className="mt-4 text-xs text-slate-500">{t("upgrade.loading", { defaultValue: "Loading…" })}</p>
            ) : !billing.enabled ? (
              <p className="mt-4 text-xs text-slate-500">
                {t("upgrade.paypalConfigNote", {
                  defaultValue:
                    "PayPal billing wires up once PAYPAL_CLIENT_ID / SECRET are configured server-side.",
                })}
              </p>
            ) : me?.kind === "sso" ? (
              <p className="mt-4 text-xs text-slate-500">
                {t("upgrade.ssoNoPaypal", {
                  defaultValue: "SSO accounts already have admin rights — no subscription needed.",
                })}
              </p>
            ) : (
              <>
                {/* Auto-renew (subscription) */}
                <div className="mt-4" data-testid="upgrade-monthly-sub-buttons">
                  <p className="text-xs font-semibold text-slate-300 mb-1">
                    {t("upgrade.autoRenew", { defaultValue: "Auto-renew monthly" })}
                  </p>
                  {!billing.plan_id_monthly ? (
                    <p className="text-xs text-slate-500">
                      {t("upgrade.paypalNoPlanMonthly", {
                        defaultValue: "PAYPAL_PLAN_ID_MONTHLY not set on the server.",
                      })}
                    </p>
                  ) : (
                    <PaypalButtons
                      kind="subscription"
                      clientId={billing.client_id}
                      planId={billing.plan_id_monthly}
                      onApprove={async (subId) => {
                        try {
                          await api.activatePaypalSubscription(subId, "monthly");
                          await refreshMe();
                          setBillingMsg(t("upgrade.subscribedOk", { defaultValue: "Subscription active — meeting creation unlocked." }));
                          const u = await api.me();
                          setMe(u);
                        } catch (e) {
                          setBillingMsg((e as Error).message);
                        }
                      }}
                    />
                  )}
                </div>
                {/* Bill-once */}
                <div className="mt-4 pt-4 border-t border-slate-700/40" data-testid="upgrade-monthly-once-buttons">
                  <p className="text-xs font-semibold text-slate-300 mb-1">
                    {t("upgrade.billOnce30", { defaultValue: "One payment — 30 days, no auto-renew" })}
                  </p>
                  <PaypalButtons
                    kind="order"
                    clientId={billing.client_id}
                    createOrder={async () => {
                      const r = await api.createPaypalOrder("monthly");
                      return r.order_id;
                    }}
                    onApprove={async (orderId) => {
                      try {
                        await api.capturePaypalOrder(orderId);
                        await refreshMe();
                        setBillingMsg(t("upgrade.monthlyOnceOk", { defaultValue: "30 days of access unlocked. Thank you!" }));
                        const u = await api.me();
                        setMe(u);
                      } catch (e) {
                        setBillingMsg((e as Error).message);
                      }
                    }}
                  />
                </div>
              </>
            )}
          </Card>

          {/* Yearly — subscribe (auto-renew) + pay-once */}
          <Card>
            <h2 className="text-lg font-semibold text-slate-100 flex items-center gap-2">
              <CalendarDays size={18} className="text-accent-500" />
              {t("upgrade.annualTitle", { defaultValue: "Yearly" })}
              <span className="ml-auto text-xs px-2 py-0.5 rounded-full bg-accent-500/20 text-accent-500 border border-accent-500/40">
                {t("upgrade.save", { defaultValue: "save 17%" })}
              </span>
            </h2>
            <p className="text-3xl font-bold text-slate-50 mt-2">
              €{billing?.annual_price ?? "20"}
              <span className="text-sm font-normal text-slate-400"> / {t("upgrade.year", { defaultValue: "year" })}</span>
            </p>
            <ul className="text-sm text-slate-400 mt-3 space-y-1 list-disc list-inside">
              <li>{t("upgrade.unlimitedMeetings", { defaultValue: "Unlimited meetings" })}</li>
              <li>{t("upgrade.choosePathYear", { defaultValue: "Auto-renew or one-time billing" })}</li>
              <li>{t("upgrade.bestValue", { defaultValue: "Two months free vs monthly" })}</li>
            </ul>
            {!billing ? (
              <p className="mt-4 text-xs text-slate-500">{t("upgrade.loading", { defaultValue: "Loading…" })}</p>
            ) : !billing.enabled ? (
              <p className="mt-4 text-xs text-slate-500">
                {t("upgrade.paypalConfigNote", {
                  defaultValue:
                    "PayPal billing wires up once PAYPAL_CLIENT_ID / SECRET are configured server-side.",
                })}
              </p>
            ) : me?.kind === "sso" ? (
              <p className="mt-4 text-xs text-slate-500">
                {t("upgrade.ssoNoPaypal", {
                  defaultValue: "SSO accounts already have admin rights — no purchase needed.",
                })}
              </p>
            ) : (
              <>
                {/* Auto-renew (subscription) */}
                <div className="mt-4" data-testid="upgrade-annual-sub-buttons">
                  <p className="text-xs font-semibold text-slate-300 mb-1">
                    {t("upgrade.autoRenewYearly", { defaultValue: "Auto-renew yearly" })}
                  </p>
                  {!billing.plan_id_annual ? (
                    <p className="text-xs text-slate-500">
                      {t("upgrade.paypalNoPlanAnnual", {
                        defaultValue: "PAYPAL_PLAN_ID_ANNUAL not set on the server.",
                      })}
                    </p>
                  ) : (
                    <PaypalButtons
                      kind="subscription"
                      clientId={billing.client_id}
                      planId={billing.plan_id_annual}
                      onApprove={async (subId) => {
                        try {
                          await api.activatePaypalSubscription(subId, "annual");
                          await refreshMe();
                          setBillingMsg(t("upgrade.subscribedOkYearly", { defaultValue: "Yearly subscription active — meeting creation unlocked." }));
                          const u = await api.me();
                          setMe(u);
                        } catch (e) {
                          setBillingMsg((e as Error).message);
                        }
                      }}
                    />
                  )}
                </div>
                {/* Bill-once */}
                <div className="mt-4 pt-4 border-t border-slate-700/40" data-testid="upgrade-annual-once-buttons">
                  <p className="text-xs font-semibold text-slate-300 mb-1">
                    {t("upgrade.billOnce365", { defaultValue: "One payment — 365 days, no auto-renew" })}
                  </p>
                  <PaypalButtons
                    kind="order"
                    clientId={billing.client_id}
                    createOrder={async () => {
                      const r = await api.createPaypalOrder("annual");
                      return r.order_id;
                    }}
                    onApprove={async (orderId) => {
                      try {
                        await api.capturePaypalOrder(orderId);
                        await refreshMe();
                        setBillingMsg(t("upgrade.purchaseOk", { defaultValue: "Annual access unlocked. Thank you!" }));
                        const u = await api.me();
                        setMe(u);
                      } catch (e) {
                        setBillingMsg((e as Error).message);
                      }
                    }}
                  />
                </div>
              </>
            )}
          </Card>
        </div>
        {subscriptionActive && (
          <Card className="mt-3" data-testid="upgrade-cancel-sub">
            <h2 className="text-base font-semibold text-slate-100">
              {t("upgrade.activeSubTitle", { defaultValue: "Active subscription" })}
            </h2>
            <p className="text-sm text-slate-400 mt-1">
              {me?.entitlement_kind === "paypal_monthly"
                ? t("upgrade.activeSubMonthly", { defaultValue: "PayPal monthly auto-renew." })
                : t("upgrade.activeSubAnnual", { defaultValue: "PayPal yearly auto-renew." })}
              {me?.entitlement_expires_at &&
                ` ${t("upgrade.activeSubUntil", {
                  date: new Date(me.entitlement_expires_at).toLocaleDateString(),
                  defaultValue: "Next renewal around {{date}}.",
                })}`}
            </p>
            <Button
              type="button"
              onClick={cancelSubscription}
              disabled={cancelling}
              className="mt-3"
              data-testid="upgrade-cancel-btn"
            >
              {cancelling
                ? t("upgrade.cancelling", { defaultValue: "Cancelling…" })
                : t("upgrade.cancelSub", { defaultValue: "Cancel auto-renewal" })}
            </Button>
          </Card>
        )}
        {billingMsg && (
          <p className="text-sm text-accent-500 mt-3" data-testid="upgrade-billing-msg">
            {billingMsg}
          </p>
        )}
        </>
      )}

      {/* Voucher redemption */}
      <Card className="mt-4">
        <h2 className="text-lg font-semibold text-slate-100 flex items-center gap-2 mb-2">
          <Ticket size={18} className="text-accent-500" />
          {t("upgrade.redeemTitle", { defaultValue: "Have a voucher?" })}
        </h2>
        {me?.kind === "sso" ? (
          <p className="text-sm text-slate-400">
            {t("upgrade.ssoNoVoucher", {
              defaultValue: "SSO accounts already have admin rights — no voucher needed.",
            })}
          </p>
        ) : (
          <form onSubmit={redeem} className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <Field id="redeem-code" label={t("upgrade.codeLabel", { defaultValue: "Voucher code" })}>
              <Input
                id="redeem-code"
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                maxLength={16}
                placeholder="ABCD2345"
                className="font-mono tracking-widest uppercase"
                data-testid="redeem-code"
              />
            </Field>
            <Button type="submit" disabled={redeeming || code.trim().length === 0} data-testid="redeem-submit">
              {redeeming ? t("upgrade.redeeming", { defaultValue: "Redeeming…" }) : t("upgrade.redeemSubmit", { defaultValue: "Redeem" })}
            </Button>
          </form>
        )}
        {redeemErr && <p className="text-sm text-red-400 mt-2">{redeemErr}</p>}
        {redeemOk && (
          <p className="text-sm text-accent-500 mt-2">
            {t("upgrade.redeemOk", {
              days: redeemOk.days,
              date: new Date(redeemOk.expiresAt).toLocaleDateString(),
              defaultValue: "Voucher accepted — admin rights granted for {{days}} days, valid until {{date}}.",
            })}
          </p>
        )}
      </Card>
    </div>
  );
}
