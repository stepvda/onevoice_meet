/**
 * person_key (contract header): `sub:<sub>` for signed-in users (LiveKit
 * identity `user-<sub>`), else `guest:<uuid>` with the uuid generated once
 * per browser and kept in sessionStorage `meetpp_guest_key`.
 */

export const GUEST_KEY_STORAGE = "meetpp_guest_key";

function uuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/** `sub:<sub>` for signed-in users (LiveKit identity `user-<sub>`), else a
 * per-browser guest uuid kept in sessionStorage. */
export function personKeyFor(identity: string | null | undefined, storage: Pick<Storage, "getItem" | "setItem"> | null = safeSession()): string {
  if (identity && identity.startsWith("user-") && identity.length > 5) return `sub:${identity.slice(5)}`;
  let key: string | null = null;
  try {
    key = storage?.getItem(GUEST_KEY_STORAGE) ?? null;
  } catch {
    key = null;
  }
  if (!key) {
    key = uuid();
    try {
      storage?.setItem(GUEST_KEY_STORAGE, key);
    } catch {
      /* private mode: key lives for this page only */
    }
  }
  return `guest:${key}`;
}

function safeSession(): Storage | null {
  try {
    return typeof sessionStorage !== "undefined" ? sessionStorage : null;
  } catch {
    return null;
  }
}

