import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Check, ExternalLink, Loader2, Plus, Send, Trash2 } from "lucide-react";
import { meetppApi } from "../../lib/meetpp/api";
import type { AgendaDraftItem, ExternalInfo } from "../../lib/meetpp/types";
import { embed, embedActive, mailPathOf } from "../../lib/embed";
import { cx } from "./ui";

/**
 * Meet++ sessions prepared from One Voice Mail's Meetings
 * (meet-integration.md §4.2): the "Prepared from Meetings" link and the
 * AI-drafted agenda of the next sitting with "Send to Meetings".
 */

/** Open the sitting in Mail: inside Mail via `meet:open-mail`, else a new tab. */
export function openInMeetings(openUrl: string | null | undefined): void {
  if (!openUrl) return;
  if (embedActive()) {
    const path = mailPathOf(openUrl);
    if (path) embed().postOpenMail(path);
    return;
  }
  window.open(openUrl, "_blank", "noopener,noreferrer");
}

/** "Prepared from Meetings", linking to the sitting in Mail. */
export function PreparedFromMeetings({ external, tone = "light", className }: { external: ExternalInfo | null | undefined; tone?: "light" | "dark"; className?: string }) {
  const { t } = useTranslation();
  if (!external) return null;
  const label = t("meetpp.external.preparedFrom", { defaultValue: "Prepared from Meetings" });
  const base = cx(
    "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium",
    tone === "dark" ? "bg-primary-700 text-slate-100" : "border border-blue-200 bg-blue-50 text-blue-800",
    className,
  );
  if (!external.open_url) return <span className={base} data-testid="meetpp-prepared-from">{label}</span>;
  return (
    <button
      type="button"
      data-testid="meetpp-prepared-from"
      className={cx(base, tone === "dark" ? "hover:bg-primary-600" : "hover:bg-blue-100")}
      title={t("meetpp.external.openInMeetings", { defaultValue: "Open in Meetings" })}
      onClick={() => openInMeetings(external.open_url)}
    >
      {label} <ExternalLink size={11} />
    </button>
  );
}

export function nextMeetingDate(external: ExternalInfo | null | undefined): string | null {
  const iso = external?.next_occurrence?.starts_at;
  if (!iso) return null;
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

/** Items worth sending: titled, trimmed, empty bodies dropped. */
export function cleanAgenda(items: AgendaDraftItem[]): AgendaDraftItem[] {
  return items
    .map((it) => ({ title: (it.title ?? "").trim(), body: (it.body ?? "").trim() }))
    .filter((it) => it.title)
    .map((it) => (it.body ? it : { title: it.title }));
}

/** Editable agenda list: title, optional details, add / remove / reorder. */
export function AgendaListEditor({ items, onChange, disabled }: { items: AgendaDraftItem[]; onChange: (items: AgendaDraftItem[]) => void; disabled?: boolean }) {
  const { t } = useTranslation();
  const set = (i: number, patch: Partial<AgendaDraftItem>) => onChange(items.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= items.length) return;
    const next = [...items];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  const btn = "rounded p-1 text-slate-500 hover:bg-slate-100 disabled:opacity-30 disabled:hover:bg-transparent";
  return (
    <div>
      <ol className="space-y-2" data-testid="next-agenda-list">
        {items.map((p, i) => (
          <li key={i} className="flex items-start gap-1.5 rounded-lg border border-slate-200 bg-white p-2">
            <span className="mt-1.5 w-5 flex-shrink-0 text-right text-sm text-slate-500">{i + 1}.</span>
            <div className="min-w-0 flex-1 space-y-1">
              <input
                className="w-full rounded border border-slate-300 px-2 py-1 text-sm text-slate-800"
                value={p.title}
                disabled={disabled}
                placeholder={t("meetpp.external.pointTitle", { defaultValue: "Point title" })}
                aria-label={t("meetpp.external.pointTitle", { defaultValue: "Point title" })}
                onChange={(e) => set(i, { title: e.target.value })}
              />
              <textarea
                className="block w-full resize-y rounded border border-slate-200 px-2 py-1 text-xs text-slate-700"
                rows={p.body ? 2 : 1}
                value={p.body ?? ""}
                disabled={disabled}
                placeholder={t("meetpp.external.pointBody", { defaultValue: "Details (optional)" })}
                aria-label={t("meetpp.external.pointBody", { defaultValue: "Details (optional)" })}
                onChange={(e) => set(i, { body: e.target.value })}
              />
            </div>
            <div className="flex flex-shrink-0 flex-col">
              <button type="button" className={btn} disabled={disabled || i === 0} onClick={() => move(i, -1)} aria-label={t("meetpp.external.moveUp", { defaultValue: "Move up" })}>
                <ArrowUp size={13} />
              </button>
              <button type="button" className={btn} disabled={disabled || i === items.length - 1} onClick={() => move(i, 1)} aria-label={t("meetpp.external.moveDown", { defaultValue: "Move down" })}>
                <ArrowDown size={13} />
              </button>
              <button
                type="button"
                className="rounded p-1 text-rose-600 hover:bg-rose-50 disabled:opacity-30"
                disabled={disabled}
                onClick={() => onChange(items.filter((_, j) => j !== i))}
                aria-label={t("meetpp.external.remove", { defaultValue: "Remove point" })}
              >
                <Trash2 size={13} />
              </button>
            </div>
          </li>
        ))}
      </ol>
      {!disabled && (
        <button
          type="button"
          className="mt-2 inline-flex items-center gap-1 text-sm font-medium text-blue-700 hover:underline"
          onClick={() => onChange([...items, { title: "", body: "" }])}
        >
          <Plus size={13} /> {t("meetpp.external.addPoint", { defaultValue: "Add point" })}
        </button>
      )}
    </div>
  );
}

/**
 * The proposal: "Draft agenda for the next meeting on {date}", editable, with
 * Send to Meetings (POST …/external/next-agenda). Controlled: the review
 * page binds it to its autosaved draft, the room's end dialog to local state.
 */
export function NextAgendaProposal({
  sid,
  external,
  items,
  onChange,
  onSent,
  loading,
  className,
}: {
  sid: string;
  external: ExternalInfo;
  items: AgendaDraftItem[];
  onChange: (items: AgendaDraftItem[]) => void;
  onSent: (external: ExternalInfo | null, agenda: AgendaDraftItem[]) => void;
  loading?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const sent = external.next_agenda_sent && !editing;
  const date = nextMeetingDate(external);
  const clean = cleanAgenda(items);

  const send = async () => {
    if (!clean.length || sending) return;
    setSending(true);
    setErr(null);
    try {
      const res = await meetppApi.sendNextAgenda(sid, clean);
      onSent(res?.external ?? { ...external, next_agenda_sent: true }, res?.next_agenda ?? clean);
      setEditing(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <section className={cx("rounded-2xl bg-white p-4 text-slate-800 shadow-sm", className)} data-testid="next-agenda-proposal">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <h3 className="min-w-0 flex-1 text-base font-semibold text-slate-900">
          {date
            ? t("meetpp.external.nextAgendaTitle", { defaultValue: "Draft agenda for the next meeting on {{date}}", date })
            : t("meetpp.external.nextAgendaTitleNoDate", { defaultValue: "Draft agenda for the next meeting" })}
        </h3>
        {external.next_agenda_sent && (
          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-800" data-testid="next-agenda-sent">
            <Check size={12} /> {t("meetpp.external.sent", { defaultValue: "Sent to Meetings" })}
          </span>
        )}
      </div>
      <p className="mb-3 text-xs text-slate-500">
        {sent
          ? t("meetpp.external.sentHint", { defaultValue: "These points were added to the next meeting in Meetings." })
          : t("meetpp.external.nextAgendaHint", { defaultValue: "Drafted by Meet++ from this meeting. Edit it, then send it to the next meeting in Meetings." })}
      </p>
      {loading ? (
        <div className="flex items-center gap-2 py-3 text-sm text-slate-500">
          <Loader2 size={14} className="animate-spin" /> {t("meetpp.external.drafting", { defaultValue: "Meet++ is drafting the agenda…" })}
        </div>
      ) : sent ? (
        <ol className="list-decimal space-y-1 pl-6 text-sm text-slate-700">
          {items.map((p, i) => (
            <li key={i}>
              <span className="font-medium">{p.title}</span>
              {p.body ? <div className="text-xs text-slate-500">{p.body}</div> : null}
            </li>
          ))}
        </ol>
      ) : (
        <>
          {items.length === 0 && (
            <p className="mb-2 text-sm text-slate-500">{t("meetpp.external.nextAgendaEmpty", { defaultValue: "Meet++ did not propose any points. Add the points for the next meeting." })}</p>
          )}
          <AgendaListEditor items={items} onChange={onChange} disabled={sending} />
        </>
      )}
      {err && (
        <p className="mt-2 text-sm text-rose-700" role="alert">
          {t("meetpp.external.sendFailed", { defaultValue: "The agenda could not be sent: {{e}}", e: err })}
        </p>
      )}
      {!loading && (
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          {sent ? (
            <button type="button" className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50" onClick={() => setEditing(true)}>
              {t("meetpp.external.edit", { defaultValue: "Edit and send again" })}
            </button>
          ) : (
            <button
              type="button"
              data-testid="next-agenda-send"
              disabled={!clean.length || sending}
              onClick={() => void send()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
            >
              {sending ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
              {sending ? t("meetpp.external.sending", { defaultValue: "Sending…" }) : t("meetpp.external.send", { defaultValue: "Send to Meetings" })}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * The proposal where the chair ends the AI meeting (room end dialog): reads
 * GET /review until the AI draft is there (finalisation runs a while), then
 * lets the chair edit and send it.
 */
export function NextAgendaFromReview({ sid, external: initial }: { sid: string; external: ExternalInfo }) {
  const [external, setExternal] = useState<ExternalInfo>(initial);
  const [items, setItems] = useState<AgendaDraftItem[] | null>(null);
  const touched = useRef(false);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;
    const load = async (attempt: number) => {
      try {
        const r = await meetppApi.getReview(sid);
        if (!alive) return;
        const ext = r.session?.external ?? r.external ?? null;
        if (ext) setExternal(ext);
        const draft = ext?.next_agenda_sent ? r.review?.next_agenda : r.final?.next_agenda;
        // Finalisation drafts the agenda; give up waiting after ~10 minutes.
        const ready = r.session?.status !== "finalising" || !!r.final?.next_agenda?.length || attempt >= 150;
        if (ready) {
          if (!touched.current) setItems(draft ?? r.final?.next_agenda ?? []);
        } else {
          timer = window.setTimeout(() => void load(attempt + 1), 4000);
        }
      } catch {
        if (alive && attempt < 150) timer = window.setTimeout(() => void load(attempt + 1), 4000);
      }
    };
    void load(0);
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  return (
    <NextAgendaProposal
      sid={sid}
      external={external}
      items={items ?? []}
      loading={items === null}
      className="border border-slate-200 shadow-none"
      onChange={(next) => {
        touched.current = true;
        setItems(next);
      }}
      onSent={(ext, agenda) => {
        if (ext) setExternal(ext);
        setItems(agenda);
      }}
    />
  );
}
