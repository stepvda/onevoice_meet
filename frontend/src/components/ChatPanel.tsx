import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Image as ImageIcon, Reply, Send, Smile, X } from "lucide-react";
import { useLocalParticipant, useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, CHAT_REACTIONS, ChatMessageDTO } from "../lib/api";

// Use the system font ("native") set so we don't depend on jsdelivr/CDN
// emoji sprite assets — those were getting blocked by our CSP / not loading
// in production, leaving an empty picker.
const EmojiPicker = lazy(() => import("emoji-picker-react"));
const EMOJI_STYLE_NATIVE = "native" as never;

const DATA_TOPIC = "meet-chat";
const TEXT_DECODER = new TextDecoder();
const TEXT_ENCODER = new TextEncoder();

interface Props {
  open: boolean;
  onClose: () => void;
}

interface ChatRefetchSignal {
  v: 1;
  type: "chat-refetch";
}

export default function ChatPanel({ open, onClose }: Props) {
  const { t } = useTranslation();
  const { roomName = "" } = useParams();
  const room = useRoomContext();
  const { localParticipant } = useLocalParticipant();

  const [messages, setMessages] = useState<ChatMessageDTO[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [text, setText] = useState("");
  const [replyTo, setReplyTo] = useState<ChatMessageDTO | null>(null);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [reactionTargetId, setReactionTargetId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textInputRef = useRef<HTMLTextAreaElement | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  const myIdentity = localParticipant?.identity ?? "";
  const myName = localParticipant?.name || myIdentity || t("common.anonymous");

  const refetch = useCallback(async () => {
    if (!roomName) return;
    try {
      const rows = await api.listChat(roomName);
      setMessages(rows);
    } catch {
      /* swallow — best-effort refresh */
    }
  }, [roomName]);

  // Initial load.
  useEffect(() => {
    if (!roomName) {
      setHistoryLoaded(true);
      return;
    }
    let cancelled = false;
    api
      .listChat(roomName)
      .then((rows) => {
        if (!cancelled) setMessages(rows);
      })
      .catch(() => {
        /* noop */
      })
      .finally(() => {
        if (!cancelled) setHistoryLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [roomName]);

  // Real-time fan-out: peers publish a small "refetch" hint over LiveKit's
  // data channel after every write, and we re-pull state from the API.
  useEffect(() => {
    if (!room) return;
    const onData = (payload: Uint8Array, _participant: unknown, _kind: unknown, topic?: string) => {
      if (topic !== DATA_TOPIC) return;
      try {
        const obj = JSON.parse(TEXT_DECODER.decode(payload)) as ChatRefetchSignal;
        if (obj?.v === 1 && obj.type === "chat-refetch") {
          refetch();
        }
      } catch {
        /* ignore malformed */
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, refetch]);

  // After our own writes, signal all other participants to refetch.
  const broadcastRefetch = useCallback(async () => {
    try {
      const payload: ChatRefetchSignal = { v: 1, type: "chat-refetch" };
      await room.localParticipant.publishData(
        TEXT_ENCODER.encode(JSON.stringify(payload)),
        { reliable: true, topic: DATA_TOPIC }
      );
    } catch {
      /* ignore — best-effort */
    }
  }, [room]);

  // Auto-scroll on growth.
  useEffect(() => {
    if (!open || !scrollerRef.current) return;
    scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
  }, [messages.length, open]);

  // Lookup table for inline reply rendering.
  const byId = useMemo(() => {
    const m = new Map<number, ChatMessageDTO>();
    for (const x of messages) m.set(x.id, x);
    return m;
  }, [messages]);

  // ─── Send actions ──────────────────────────────────────────────────

  async function sendText() {
    const v = text.trim();
    if (!v || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const row = await api.postChat(roomName, {
        sender_identity: myIdentity,
        sender_name: myName,
        message: v,
        reply_to_id: replyTo?.id ?? null,
      });
      setMessages((cur) => [...cur, row]);
      setText("");
      setReplyTo(null);
      await broadcastRefetch();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function uploadImage(file: File) {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const row = await api.postChatAttachment(roomName, {
        sender_identity: myIdentity,
        sender_name: myName,
        message: text.trim() || undefined,
        reply_to_id: replyTo?.id ?? null,
        file,
      });
      setMessages((cur) => [...cur, row]);
      setText("");
      setReplyTo(null);
      await broadcastRefetch();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleReaction(messageId: number, emoji: string) {
    const msg = byId.get(messageId);
    if (!msg) return;
    const mine = msg.reactions.find((r) => r.reactor_identity === myIdentity);
    try {
      if (mine && mine.emoji === emoji) {
        await api.deleteChatReaction(roomName, messageId, myIdentity);
      } else {
        await api.putChatReaction(roomName, messageId, {
          reactor_identity: myIdentity,
          reactor_name: myName,
          emoji,
        });
      }
      await refetch();
      await broadcastRefetch();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setReactionTargetId(null);
    }
  }

  function onEmojiPicked(emoji: string) {
    const el = textInputRef.current;
    if (!el) {
      setText((cur) => cur + emoji);
      return;
    }
    const start = el.selectionStart ?? text.length;
    const end = el.selectionEnd ?? text.length;
    setText(text.slice(0, start) + emoji + text.slice(end));
    requestAnimationFrame(() => {
      el.focus();
      const pos = start + emoji.length;
      el.setSelectionRange(pos, pos);
    });
  }

  if (!open) return null;

  return (
    <aside
      data-testid="chat-panel"
      className="h-full w-full sm:w-96 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
      role="complementary"
      aria-label={t("chatPanel.ariaLabel")}
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">{t("chatPanel.title")}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("chatPanel.close")}
          data-testid="chat-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <div
        ref={scrollerRef}
        className="flex-1 overflow-y-auto px-3 py-3 flex flex-col gap-2"
        data-testid="chat-messages"
      >
        {historyLoaded && messages.length === 0 && (
          <p className="text-sm text-slate-400 px-1">{t("chatPanel.empty")}</p>
        )}
        {messages.map((m) => {
          const mine = m.sender_identity === myIdentity;
          const parent = m.reply_to_id != null ? byId.get(m.reply_to_id) : null;
          return (
            <MessageBubble
              key={m.id}
              msg={m}
              mine={mine}
              parent={parent ?? null}
              myIdentity={myIdentity}
              reactionPickerOpen={reactionTargetId === m.id}
              onOpenReactionPicker={() =>
                setReactionTargetId(reactionTargetId === m.id ? null : m.id)
              }
              onCloseReactionPicker={() => setReactionTargetId(null)}
              onPickReaction={(e) => toggleReaction(m.id, e)}
              onReply={() => setReplyTo(m)}
            />
          );
        })}
      </div>

      {replyTo && (
        <div
          data-testid="chat-reply-target"
          className="px-3 py-2 border-t border-primary-700 bg-primary-900/70 flex items-start gap-2 text-xs"
        >
          <Reply size={14} className="text-slate-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="text-slate-300">
              {t("chatPanel.replyingTo", { name: replyTo.sender_name, defaultValue: "Replying to {{name}}" })}
            </div>
            <div className="text-slate-400 truncate">{replyTo.message || (replyTo.attachment ? "🖼️ image" : "")}</div>
          </div>
          <button
            type="button"
            onClick={() => setReplyTo(null)}
            aria-label={t("chatPanel.cancelReply", { defaultValue: "Cancel reply" })}
            className="p-1 rounded hover:bg-primary-700 text-slate-400"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {err && (
        <div data-testid="chat-error" className="px-3 py-2 text-xs text-red-400 border-t border-primary-700">
          {err}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          sendText();
        }}
        className="border-t border-primary-700 p-2 flex items-end gap-2 relative"
      >
        <input
          ref={fileInputRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,image/gif"
          className="hidden"
          data-testid="chat-file-input"
          aria-label={t("chatPanel.attachImage", { defaultValue: "Attach image" })}
          title={t("chatPanel.attachImage", { defaultValue: "Attach image" })}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadImage(f);
            if (fileInputRef.current) fileInputRef.current.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={busy}
          title={t("chatPanel.attachImage", { defaultValue: "Attach image" })}
          aria-label={t("chatPanel.attachImage", { defaultValue: "Attach image" })}
          data-testid="chat-attach"
          className="p-2 rounded-lg text-slate-300 hover:bg-primary-700 disabled:opacity-50"
        >
          <ImageIcon size={18} />
        </button>
        <button
          type="button"
          onClick={() => setEmojiOpen((v) => !v)}
          disabled={busy}
          title={t("chatPanel.emojiTitle", { defaultValue: "Insert emoji" })}
          aria-label={t("chatPanel.emojiTitle", { defaultValue: "Insert emoji" })}
          data-testid="chat-emoji-toggle"
          className={[
            "p-2 rounded-lg text-slate-300 hover:bg-primary-700 disabled:opacity-50",
            emojiOpen ? "bg-primary-700" : "",
          ].join(" ")}
        >
          <Smile size={18} />
        </button>
        <textarea
          ref={textInputRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={1}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              sendText();
            }
          }}
          placeholder={t("chatPanel.placeholder")}
          aria-label={t("chatPanel.messageAria")}
          data-testid="chat-input"
          className="flex-1 px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none max-h-32"
        />
        <button
          type="submit"
          disabled={busy || !text.trim()}
          data-testid="chat-send"
          className="p-2 rounded-lg bg-accent-500 text-white hover:bg-accent-600 disabled:opacity-50"
          aria-label={t("chatPanel.send")}
        >
          <Send size={18} />
        </button>

        {emojiOpen && (
          <div className="absolute bottom-14 right-2 z-30 shadow-2xl">
            <Suspense fallback={<div className="text-xs text-slate-400 p-2">…</div>}>
              <EmojiPicker
                onEmojiClick={(e) => {
                  onEmojiPicked(e.emoji);
                  setEmojiOpen(false);
                }}
                width={320}
                height={400}
                emojiStyle={EMOJI_STYLE_NATIVE}
                theme={"dark" as never}
              />
            </Suspense>
          </div>
        )}
      </form>
    </aside>
  );
}

// ─── Message bubble ─────────────────────────────────────────────────────

interface BubbleProps {
  msg: ChatMessageDTO;
  mine: boolean;
  parent: ChatMessageDTO | null;
  myIdentity: string;
  reactionPickerOpen: boolean;
  onOpenReactionPicker: () => void;
  onCloseReactionPicker: () => void;
  onPickReaction: (emoji: string) => void;
  onReply: () => void;
}

function MessageBubble({
  msg,
  mine,
  parent,
  myIdentity,
  reactionPickerOpen,
  onOpenReactionPicker,
  onCloseReactionPicker,
  onPickReaction,
  onReply,
}: BubbleProps) {
  const { t } = useTranslation();
  const grouped = useMemo(() => {
    const map = new Map<string, { count: number; mine: boolean; names: string[] }>();
    for (const r of msg.reactions) {
      const cur = map.get(r.emoji) ?? { count: 0, mine: false, names: [] };
      cur.count += 1;
      if (r.reactor_identity === myIdentity) cur.mine = true;
      cur.names.push(r.reactor_name);
      map.set(r.emoji, cur);
    }
    return [...map.entries()].map(([emoji, v]) => ({ emoji, ...v }));
  }, [msg.reactions, myIdentity]);

  return (
    <div
      data-testid={`chat-msg-${msg.id}`}
      className={mine ? "self-end max-w-[85%]" : "self-start max-w-[85%]"}
    >
      {!mine && <div className="text-xs text-slate-400 mb-0.5 px-1">{msg.sender_name}</div>}

      <div className="group relative">
        <div
          className={[
            "rounded-lg px-3 py-2 text-sm break-words",
            mine
              ? "bg-accent-500/30 text-slate-50 border border-accent-500/40"
              : "bg-primary-800 text-slate-100 border border-primary-700",
          ].join(" ")}
        >
          {parent && (
            <div className="mb-1 pl-2 border-l-2 border-slate-500/50 text-xs text-slate-400">
              <div className="font-medium text-slate-300 truncate">{parent.sender_name}</div>
              <div className="truncate">{parent.message || (parent.attachment ? "🖼️ image" : "")}</div>
            </div>
          )}
          {msg.attachment?.type?.startsWith("image/") && (
            <a
              href={msg.attachment.url}
              target="_blank"
              rel="noopener noreferrer"
              className="block mb-1"
            >
              <img
                src={msg.attachment.url}
                alt={msg.attachment.name ?? ""}
                className="max-w-full max-h-72 rounded-md border border-primary-700"
              />
            </a>
          )}
          {msg.message && (
            <div className="prose prose-invert prose-sm max-w-none [&_p]:my-1 [&_pre]:my-1 [&_ul]:my-1 [&_ol]:my-1">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  a: ({ node: _node, ...props }) => (
                    <a {...props} target="_blank" rel="noopener noreferrer" />
                  ),
                }}
              >
                {msg.message}
              </ReactMarkdown>
            </div>
          )}
        </div>

        <div
          className={[
            "absolute -top-3 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity",
            mine ? "left-2" : "right-2",
          ].join(" ")}
        >
          <button
            type="button"
            onClick={onOpenReactionPicker}
            data-testid={`chat-react-toggle-${msg.id}`}
            title={t("chatPanel.addReaction", { defaultValue: "Add reaction" })}
            aria-label={t("chatPanel.addReaction", { defaultValue: "Add reaction" })}
            className="p-1 rounded-full bg-primary-700 hover:bg-primary-600 text-slate-200 border border-primary-600"
          >
            <Smile size={12} />
          </button>
          <button
            type="button"
            onClick={onReply}
            data-testid={`chat-reply-${msg.id}`}
            title={t("chatPanel.reply", { defaultValue: "Reply" })}
            aria-label={t("chatPanel.reply", { defaultValue: "Reply" })}
            className="p-1 rounded-full bg-primary-700 hover:bg-primary-600 text-slate-200 border border-primary-600"
          >
            <Reply size={12} />
          </button>
        </div>

        {reactionPickerOpen && (
          <div
            data-testid={`chat-react-picker-${msg.id}`}
            className={[
              "absolute z-20 -top-10 bg-primary-800 border border-primary-600 rounded-full shadow-xl flex items-center gap-0.5 px-1.5 py-1",
              mine ? "left-0" : "right-0",
            ].join(" ")}
            onMouseLeave={onCloseReactionPicker}
          >
            {CHAT_REACTIONS.map((emoji) => (
              <button
                key={emoji}
                type="button"
                onClick={() => onPickReaction(emoji)}
                data-testid={`chat-react-${msg.id}-${emoji}`}
                className="text-base px-1 rounded hover:bg-primary-700"
              >
                {emoji}
              </button>
            ))}
          </div>
        )}
      </div>

      {grouped.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1 px-1">
          {grouped.map((g) => (
            <button
              key={g.emoji}
              type="button"
              onClick={() => onPickReaction(g.emoji)}
              title={g.names.join(", ")}
              data-testid={`chat-reaction-${msg.id}-${g.emoji}`}
              className={[
                "inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-full border",
                g.mine
                  ? "bg-accent-500/20 border-accent-500/50 text-slate-100"
                  : "bg-primary-800 border-primary-700 text-slate-200",
              ].join(" ")}
            >
              <span>{g.emoji}</span>
              <span className="text-[10px] text-slate-300">{g.count}</span>
            </button>
          ))}
        </div>
      )}

      <div className="text-[10px] text-slate-500 mt-0.5 px-1">
        {new Date(msg.sent_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
      </div>
    </div>
  );
}
