import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, MessageSquare } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ChatMessageDTO } from "../lib/api";
import { bootstrapFromOneWitysk, isAuthenticated } from "../lib/auth";
import { Button, Card } from "../components/ui";

/** Read-only transcript of a (typically closed) meeting's chat. Owner-only:
 * the underlying API endpoint requires the user's JWT and verifies ownership.
 * Used from the "Closed meetings" list on Home. */
export default function MeetingChat() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { meetingId = "" } = useParams();
  const [rows, setRows] = useState<ChatMessageDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isAuthenticated()) {
        const tok = await bootstrapFromOneWitysk();
        if (!tok) {
          if (!cancelled) {
            setErr(t("meetingChat.signInFirst", { defaultValue: "Sign in on one.witysk.org first." }));
            setLoading(false);
          }
          return;
        }
      }
      try {
        const list = await api.listMeetingChat(meetingId);
        if (!cancelled) setRows(list);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [meetingId, t]);

  const byId = useMemo(() => {
    const m = new Map<number, ChatMessageDTO>();
    for (const x of rows) m.set(x.id, x);
    return m;
  }, [rows]);

  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="meeting-chat-page">
      <div className="flex items-center gap-3 mb-4">
        <Button type="button" variant="ghost" size="sm" onClick={() => navigate("/")}>
          <ArrowLeft size={16} />
          {t("meetingChat.back", { defaultValue: "Back" })}
        </Button>
        <h1 className="text-xl font-bold text-slate-50 flex items-center gap-2">
          <MessageSquare size={20} className="text-accent-500" />
          {t("meetingChat.title", { defaultValue: "Meeting chat" })}
        </h1>
      </div>

      {loading && (
        <Card>
          <p className="text-slate-300">{t("meetingChat.loading", { defaultValue: "Loading…" })}</p>
        </Card>
      )}
      {!loading && err && (
        <Card>
          <p className="text-red-400" data-testid="meeting-chat-error">
            {err}
          </p>
        </Card>
      )}
      {!loading && !err && rows.length === 0 && (
        <Card data-testid="meeting-chat-empty">
          <p className="text-slate-300">
            {t("meetingChat.empty", { defaultValue: "No chat messages were sent in this meeting." })}
          </p>
        </Card>
      )}

      {!loading && rows.length > 0 && (
        <Card className="flex flex-col gap-2" data-testid="meeting-chat-transcript">
          {rows.map((m) => {
            const parent = m.reply_to_id != null ? byId.get(m.reply_to_id) : null;
            return (
              <div key={m.id} className="border-b border-primary-700/60 last:border-b-0 pb-2 last:pb-0">
                <div className="flex items-baseline gap-2">
                  <span className="text-sm font-medium text-slate-100">{m.sender_name}</span>
                  <span className="text-xs text-slate-500">
                    {new Date(m.sent_at).toLocaleString()}
                  </span>
                </div>
                {parent && (
                  <div className="ml-3 mt-1 pl-2 border-l-2 border-slate-500/40 text-xs text-slate-400">
                    <span className="font-medium text-slate-300">{parent.sender_name}: </span>
                    <span className="truncate">{parent.message || (parent.attachment ? "🖼️ image" : "")}</span>
                  </div>
                )}
                {m.attachment?.type?.startsWith("image/") && (
                  <a
                    href={m.attachment.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-block mt-1"
                  >
                    <img
                      src={m.attachment.url}
                      alt={m.attachment.name ?? ""}
                      className="max-w-full max-h-72 rounded-md border border-primary-700"
                    />
                  </a>
                )}
                {m.message && (
                  <div className="prose prose-invert prose-sm max-w-none mt-1 [&_p]:my-1 [&_pre]:my-1 [&_ul]:my-1 [&_ol]:my-1">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ node: _node, ...props }) => (
                          <a {...props} target="_blank" rel="noopener noreferrer" />
                        ),
                      }}
                    >
                      {m.message}
                    </ReactMarkdown>
                  </div>
                )}
                {m.reactions.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {Object.entries(
                      m.reactions.reduce<Record<string, number>>((acc, r) => {
                        acc[r.emoji] = (acc[r.emoji] ?? 0) + 1;
                        return acc;
                      }, {})
                    ).map(([emoji, count]) => (
                      <span
                        key={emoji}
                        className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-full bg-primary-800 border border-primary-700 text-slate-200"
                      >
                        <span>{emoji}</span>
                        <span className="text-[10px] text-slate-400">{count}</span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );
}
