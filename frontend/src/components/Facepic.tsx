/**
 * Facepic — circular user avatar fetched authenticated from one.witysk.org.
 * Reusable wherever we render someone's face. Supports a `live` flag that
 * decorates the circle with a 2 px purple/pink ring and a "LIVE" pill at the
 * bottom — the visual marker for users currently in the TI Café.
 *
 * Image bytes are fetched with the same Bearer token used elsewhere; the
 * fetch happens client-side so the session's IP-binding stays valid (no
 * server-to-server hop). `cache: "no-store"` sidesteps the upstream's lack
 * of `Vary: Authorization`, which otherwise leaves stale entries in the
 * browser's HTTP cache.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getAccessToken } from "../lib/auth";

const ONE_WITYSK = "https://one.witysk.org";

export interface FacepicUser {
  id: number;
  username: string;
  name: string | null;
  facepic_path: string | null;
}

interface Props {
  user: FacepicUser;
  size?: number; // px, default 40 (matches existing TI Café list)
  live?: boolean;
  className?: string;
}

export default function Facepic({ user, size = 40, live = false, className = "" }: Props) {
  const { t } = useTranslation();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const tok = getAccessToken();

  const rel = user.facepic_path?.replace(/^\/+/, "") ?? "";
  const url = rel ? `${ONE_WITYSK}/api/files/${rel}` : "";

  useEffect(() => {
    if (!url || !tok) return;
    let cancelled = false;
    let blob: string | null = null;
    fetch(url, {
      headers: { Authorization: `Bearer ${tok}` },
      cache: "no-store",
    })
      .then(async (r) => {
        if (!r.ok) {
          const detail = await r.text().catch(() => "");
          throw new Error(`HTTP ${r.status}${detail ? `: ${detail.slice(0, 80)}` : ""}`);
        }
        return r.blob();
      })
      .then((b) => {
        if (cancelled) return;
        blob = URL.createObjectURL(b);
        setBlobUrl(blob);
        setErrorDetail(null);
      })
      .catch((e) => {
        if (cancelled) return;
        // eslint-disable-next-line no-console
        console.warn(`Facepic load failed for ${user.username}`, e);
        setErrorDetail((e as Error).message || "fetch_error");
      });
    return () => {
      cancelled = true;
      if (blob) URL.revokeObjectURL(blob);
    };
  }, [url, tok, user.username]);

  const initials = (user.name || user.username || "??").slice(0, 2).toUpperCase();
  // 2 px ring on the outer wrapper (live), inset image so the visual diameter
  // is unchanged for non-live users.
  const wrapperStyle = live
    ? { boxShadow: "0 0 0 2px #c026d3" } // bright fuchsia/purple
    : undefined;

  const tip = errorDetail ? `${url}\n${errorDetail}` : url ? `Loading: ${url}` : "";

  return (
    <div
      className={`relative inline-block ${className}`}
      style={{ width: size, height: size }}
      title={tip}
    >
      <div
        className="absolute inset-0 rounded-full overflow-hidden border border-primary-700"
        style={wrapperStyle}
      >
        {blobUrl ? (
          <img
            src={blobUrl}
            alt={user.name || user.username}
            className="w-full h-full object-cover"
          />
        ) : (
          <div
            className="w-full h-full bg-primary-600 flex items-center justify-center font-semibold text-slate-50"
            style={{ fontSize: size * 0.34 }}
          >
            {initials}
          </div>
        )}
      </div>
      {live && (
        <span
          className="absolute left-1/2 -translate-x-1/2 -bottom-1 px-1.5 py-px rounded-sm text-[8px] font-extrabold tracking-wider text-white pointer-events-none"
          style={{ backgroundColor: "#c026d3" }}
          aria-label={t("tiCafe.live")}
        >
          {t("tiCafe.live")}
        </span>
      )}
    </div>
  );
}
