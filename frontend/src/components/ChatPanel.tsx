import { useEffect, useRef, useState } from "react";
import { Send, X } from "lucide-react";
import { useChat, useLocalParticipant } from "@livekit/components-react";

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function ChatPanel({ open, onClose }: Props) {
  const { send, chatMessages, isSending } = useChat();
  const { localParticipant } = useLocalParticipant();
  const [text, setText] = useState("");
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll on new message.
  useEffect(() => {
    if (!open || !scrollerRef.current) return;
    scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
  }, [chatMessages.length, open]);

  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const v = text.trim();
    if (!v) return;
    try {
      await send(v);
      setText("");
    } catch {
      /* keep text so user can retry */
    }
  }

  return (
    <aside
      data-testid="chat-panel"
      className="fixed top-0 right-0 h-screen w-full sm:w-96 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col z-30"
      role="dialog"
      aria-label="Meeting chat"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">Chat</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close chat"
          data-testid="chat-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <div
        ref={scrollerRef}
        className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-3"
        data-testid="chat-messages"
      >
        {chatMessages.length === 0 && (
          <p className="text-sm text-slate-400">No messages yet — say hello.</p>
        )}
        {chatMessages.map((m) => {
          const mine = m.from?.identity === localParticipant?.identity;
          const name = m.from?.name || m.from?.identity || "anonymous";
          return (
            <div
              key={m.timestamp + (m.from?.identity ?? "")}
              className={mine ? "self-end max-w-[80%]" : "self-start max-w-[80%]"}
            >
              {!mine && (
                <div className="text-xs text-slate-400 mb-0.5">{name}</div>
              )}
              <div
                className={[
                  "rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words",
                  mine
                    ? "bg-accent-500/30 text-slate-50 border border-accent-500/40"
                    : "bg-primary-800 text-slate-100 border border-primary-700",
                ].join(" ")}
              >
                {m.message}
              </div>
              <div className="text-[10px] text-slate-500 mt-0.5">
                {new Date(m.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </div>
            </div>
          );
        })}
      </div>

      <form
        onSubmit={submit}
        className="border-t border-primary-700 p-3 flex gap-2"
      >
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Type a message"
          aria-label="Chat message"
          data-testid="chat-input"
          className="flex-1 px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
        />
        <button
          type="submit"
          disabled={isSending || !text.trim()}
          data-testid="chat-send"
          className="px-3 py-2 rounded-lg bg-accent-500 text-white hover:bg-accent-600 disabled:opacity-50"
          aria-label="Send"
        >
          <Send size={16} />
        </button>
      </form>
    </aside>
  );
}
