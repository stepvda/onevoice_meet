/**
 * Shared "current signed-in user" hook.
 *
 * All gating decisions across the SPA — sidebar visibility, create-meeting
 * card visibility, voucher-admin route, account page — depend on the meet
 * user's `kind` and `is_admin`. Fetching /v1/me from each component would
 * fan out to multiple identical requests on every navigation; instead we
 * keep a single module-level cache and let consumers subscribe.
 *
 * The cache is invalidated by:
 *   - explicit `refreshMe()` (after login, signup, profile edit, voucher
 *     redemption, account deletion, …)
 *   - `clearMe()` on logout.
 */
import { useEffect, useState } from "react";
import { api, MeOut } from "./api";
import { bootstrapFromOneWitysk, isAuthenticated } from "./auth";

let cached: MeOut | null = null;
let inflight: Promise<MeOut | null> | null = null;
const subscribers: Set<(me: MeOut | null) => void> = new Set();

function notify() {
  for (const s of subscribers) s(cached);
}

async function fetchMe(): Promise<MeOut | null> {
  if (!isAuthenticated()) {
    const tok = await bootstrapFromOneWitysk();
    if (!tok) {
      cached = null;
      notify();
      return null;
    }
  }
  try {
    const u = await api.me();
    cached = u;
    notify();
    return u;
  } catch {
    cached = null;
    notify();
    return null;
  }
}

/** Force a re-fetch — call after any action that mutates the user record
 *  (login, signup, profile edit, voucher redemption, …). */
export async function refreshMe(): Promise<MeOut | null> {
  inflight = fetchMe();
  return inflight;
}

/** Clear the cache — call on logout / account deletion. */
export function clearMe(): void {
  cached = null;
  inflight = null;
  notify();
}

/** Synchronous read of whatever's currently in the cache. `null` when
 *  the cache is cold OR a fetch hasn't been started yet. Use this for
 *  component initial-state (e.g. pre-filling a form field) where you
 *  want the value available on the first render without waiting for
 *  an effect to fire. Pair with `useMe()` to also react when the
 *  cache fills later. */
export function getCachedMe(): MeOut | null {
  return cached;
}

/** Hook: returns the cached user (or null) and a `loading` flag. Triggers
 *  a fetch on first mount if there's nothing cached yet. */
export function useMe(): { me: MeOut | null; loading: boolean } {
  const [, setTick] = useState(0);
  const [loading, setLoading] = useState(cached === null);

  useEffect(() => {
    const sub = () => setTick((n) => n + 1);
    subscribers.add(sub);
    if (cached === null && inflight === null) {
      setLoading(true);
      inflight = fetchMe().finally(() => {
        setLoading(false);
        inflight = null;
      });
    } else if (inflight !== null) {
      void inflight.finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
    return () => {
      subscribers.delete(sub);
    };
  }, []);

  return { me: cached, loading };
}
