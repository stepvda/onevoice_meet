import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, X as XIcon } from "lucide-react";
import { api, type PendingJoiner } from "../lib/api";

interface Props {
  meetingId: string;
  open: boolean;
  onClose: () => void;
  onCountChange?: (n: number) => void;
}

/**
 * Owner-only side panel listing participants waiting in the waiting room.
 * Polls the backend every 3 seconds; each entry has Admit / Deny buttons.
 */
export default function PendingJoinersPanel({ meetingId, open, onClose, onCountChange }: Props) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<PendingJoiner[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const r = await api.listPendingJoiners(meetingId);
        if (cancelled) return;
        setRows(r);
        onCountChange?.(r.length);
      } catch (e) {
        if (cancelled) return;
        setErr((e as Error).message);
      }
    };
    void fetchOnce();
    const id = window.setInterval(() => void fetchOnce(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [meetingId, onCountChange]);

  if (!open) return null;

  async function admit(p: PendingJoiner) {
    setBusy(p.wait_token);
    setErr(null);
    try {
      await api.admitPending(meetingId, p.wait_token);
      setRows((cur) => cur.filter((x) => x.wait_token !== p.wait_token));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }
  async function deny(p: PendingJoiner) {
    setBusy(p.wait_token);
    setErr(null);
    try {
      await api.denyPending(meetingId, p.wait_token);
      setRows((cur) => cur.filter((x) => x.wait_token !== p.wait_token));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <aside
      data-testid="pending-joiners"
      role="complementary"
      aria-label={t("pending.title")}
      className="h-full w-full sm:w-80 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">
          {t("pending.title")} <span className="text-slate-400">({rows.length})</span>
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("pending.close")}
          data-testid="pending-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <XIcon size={18} />
        </button>
      </header>
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {err && <div className="text-red-400 text-sm">{err}</div>}
        {rows.length === 0 && !err && (
          <p className="text-sm text-slate-400">{t("pending.empty")}</p>
        )}
        {rows.map((p) => (
          <div
            key={p.wait_token}
            data-testid={`pending-row-${p.wait_token}`}
            className="rounded-md border border-primary-700 bg-primary-800/60 px-3 py-2"
          >
            <div className="text-sm font-medium text-slate-100 truncate">{p.display_name}</div>
            {p.email && (
              <div className="text-xs text-slate-400 truncate privacy-blur-email">{p.email}</div>
            )}
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                onClick={() => admit(p)}
                disabled={busy === p.wait_token}
                data-testid={`pending-admit-${p.wait_token}`}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-accent-500 hover:bg-accent-600 text-white text-xs disabled:opacity-50"
              >
                <Check size={14} /> {t("pending.admit")}
              </button>
              <button
                type="button"
                onClick={() => deny(p)}
                disabled={busy === p.wait_token}
                data-testid={`pending-deny-${p.wait_token}`}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-red-700 hover:bg-red-800 text-white text-xs disabled:opacity-50"
              >
                <XIcon size={14} /> {t("pending.deny")}
              </button>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
