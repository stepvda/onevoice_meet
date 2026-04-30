/**
 * PayPal JS Buttons SDK wrapper.
 *
 * Two button kinds:
 *   - "subscription" — renders the recurring subscribe button. Requires
 *     a `plan_id` (server-side pre-created PayPal plan).
 *   - "order" — renders the one-shot pay button. createOrder + onApprove
 *     hit our backend; PayPal's flow runs client-side.
 *
 * Loads the SDK on demand (only when the button mounts), so users not on
 * /upgrade don't pull ~80 KB of vendor JS.
 */
import { useEffect, useRef, useState } from "react";

type Variant =
  | { kind: "subscription"; clientId: string; planId: string; onApprove: (subId: string) => void; onError?: (e: unknown) => void }
  | { kind: "order"; clientId: string; createOrder: () => Promise<string>; onApprove: (orderId: string) => Promise<void>; onError?: (e: unknown) => void };

declare global {
  interface Window {
    paypal?: unknown;
    paypal_subs?: unknown;
  }
}

// PayPal's SDK can only honour ONE `intent` per script load — subscription
// SDKs reject order-button configs and vice-versa. To render both kinds of
// buttons on the same page we load two SDKs into separate `data-namespace`
// globals (`paypal` for capture orders, `paypal_subs` for subscriptions),
// keyed by intent so each promise resolves to the matching namespace object.
const sdkPromises: Partial<Record<"subscription" | "capture", Promise<unknown>>> = {};

function loadSdk(clientId: string, intent: "subscription" | "capture"): Promise<unknown> {
  const cached = sdkPromises[intent];
  if (cached) return cached;
  const ns = intent === "subscription" ? "paypal_subs" : "paypal";
  const p = new Promise((resolve, reject) => {
    const selector = `script[data-paypal-sdk="${intent}"]`;
    const existing = document.querySelector(selector) as HTMLScriptElement | null;
    if (existing) {
      // Already in flight from a parallel mount — re-await it. If it has
      // finished loading, the global is set; resolve immediately.
      const ready = (window as unknown as Record<string, unknown>)[ns];
      if (ready) {
        resolve(ready);
        return;
      }
      existing.addEventListener("load", () =>
        resolve((window as unknown as Record<string, unknown>)[ns])
      );
      existing.addEventListener("error", () => reject(new Error("PayPal SDK failed to load")));
      return;
    }
    const params = new URLSearchParams({
      "client-id": clientId,
      currency: "EUR",
      // For subscriptions PayPal requires `vault=true&intent=subscription`.
      ...(intent === "subscription"
        ? { vault: "true", intent: "subscription" }
        : { intent: "capture" }),
    });
    const s = document.createElement("script");
    s.src = `https://www.paypal.com/sdk/js?${params.toString()}`;
    s.dataset.paypalSdk = intent;
    // `data-namespace` makes PayPal expose its API under window[ns] so the
    // two SDKs (subscription + capture) coexist without overwriting each other.
    s.dataset.namespace = ns;
    s.async = true;
    s.onload = () => resolve((window as unknown as Record<string, unknown>)[ns]);
    s.onerror = () => reject(new Error("PayPal SDK failed to load"));
    document.head.appendChild(s);
  });
  sdkPromises[intent] = p;
  return p;
}

export default function PaypalButtons(props: Variant) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const intent = props.kind === "subscription" ? "subscription" : "capture";
    loadSdk(props.clientId, intent)
      .then((paypal) => {
        if (cancelled || !containerRef.current) return;
        // Clear any previous render (StrictMode mounts effects twice).
        containerRef.current.innerHTML = "";
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const pp = paypal as any;
        const config: Record<string, unknown> =
          props.kind === "subscription"
            ? {
                style: { layout: "horizontal", color: "blue", shape: "rect", label: "subscribe" },
                createSubscription: (_data: unknown, actions: { subscription: { create: (o: { plan_id: string }) => unknown } }) =>
                  actions.subscription.create({ plan_id: props.planId }),
                onApprove: (data: { subscriptionID: string }) => props.onApprove(data.subscriptionID),
                onError: (e: unknown) => {
                  setErr(String(e));
                  props.onError?.(e);
                },
              }
            : {
                style: { layout: "horizontal", color: "gold", shape: "rect", label: "pay" },
                createOrder: () => props.createOrder(),
                onApprove: async (data: { orderID: string }) => {
                  await props.onApprove(data.orderID);
                },
                onError: (e: unknown) => {
                  setErr(String(e));
                  props.onError?.(e);
                },
              };
        pp.Buttons(config).render(containerRef.current);
      })
      .catch((e) => {
        if (!cancelled) setErr((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.clientId, props.kind, "planId" in props ? props.planId : undefined]);

  return (
    <div>
      <div ref={containerRef} />
      {err && <p className="text-xs text-red-400 mt-2">{err}</p>}
    </div>
  );
}
