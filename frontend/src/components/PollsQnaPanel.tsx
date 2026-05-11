import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, MessageCircleQuestion, ThumbsUp, Trash2, Vote, X } from "lucide-react";
import { useLocalParticipant, useRoomContext } from "@livekit/components-react";
import { api, type PollDTO, type QuestionDTO } from "../lib/api";

export const POLLS_TOPIC = "meet-polls";
const ENCODER_BROADCAST = new TextEncoder();

/** Broadcast a tiny ping on the meet-polls topic so other clients can
 * auto-open their Polls/Q&A panel. Body is just a kind discriminator. */
async function broadcastPollsActivity(
  room: ReturnType<typeof useRoomContext>,
  kind: "poll" | "question",
) {
  try {
    await room.localParticipant.publishData(
      ENCODER_BROADCAST.encode(JSON.stringify({ v: 1, kind })),
      { reliable: true, topic: POLLS_TOPIC },
    );
  } catch {
    /* offline / non-critical — the polling refresh will still pick it up */
  }
}

interface Props {
  open: boolean;
  onClose: () => void;
  meetingId: string | null;
  isModerator: boolean;
}

type Tab = "polls" | "qna";

const POLL_REFRESH_MS = 4000;

/**
 * Combined Polls + Q&A side panel. Both lists poll the backend every 4
 * seconds while the panel is open — good enough resolution for human
 * voting; cheaper than wiring a third data-channel topic.
 */
export default function PollsQnaPanel({ open, onClose, meetingId, isModerator }: Props) {
  const { t } = useTranslation();
  const { localParticipant } = useLocalParticipant();
  const room = useRoomContext();
  const [tab, setTab] = useState<Tab>("polls");
  const [polls, setPolls] = useState<PollDTO[]>([]);
  const [questions, setQuestions] = useState<QuestionDTO[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !meetingId) return;
    let cancelled = false;
    const fetchAll = async () => {
      try {
        const [p, q] = await Promise.all([
          api.listPolls(meetingId),
          api.listQuestions(meetingId),
        ]);
        if (cancelled) return;
        setPolls(p);
        setQuestions(q);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      }
    };
    void fetchAll();
    const id = window.setInterval(() => void fetchAll(), POLL_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [open, meetingId]);

  if (!open) return null;
  const me = localParticipant?.identity ?? "anon";
  const myName = localParticipant?.name || me;

  return (
    <aside
      data-testid="polls-qna-panel"
      role="complementary"
      aria-label={t("polls.title", { defaultValue: "Polls & Q&A" })}
      className="h-full w-full sm:w-80 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">
          {t("polls.title", { defaultValue: "Polls & Q&A" })}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("polls.close", { defaultValue: "Close" })}
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>
      <div className="flex border-b border-primary-700 text-sm">
        <button
          type="button"
          onClick={() => setTab("polls")}
          aria-pressed={tab === "polls" ? "true" : "false"}
          className={[
            "flex-1 py-2 inline-flex items-center justify-center gap-1.5",
            tab === "polls" ? "text-accent-400 border-b-2 border-accent-500" : "text-slate-400 hover:text-slate-200",
          ].join(" ")}
        >
          <Vote size={14} /> {t("polls.tabPolls", { defaultValue: "Polls" })}
        </button>
        <button
          type="button"
          onClick={() => setTab("qna")}
          aria-pressed={tab === "qna" ? "true" : "false"}
          className={[
            "flex-1 py-2 inline-flex items-center justify-center gap-1.5",
            tab === "qna" ? "text-accent-400 border-b-2 border-accent-500" : "text-slate-400 hover:text-slate-200",
          ].join(" ")}
        >
          <MessageCircleQuestion size={14} /> {t("polls.tabQna", { defaultValue: "Q&A" })}
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {err && <p className="text-xs text-red-400">{err}</p>}
        {tab === "polls" ? (
          <PollsTab
            polls={polls}
            meetingId={meetingId}
            isModerator={isModerator}
            me={me}
            setErr={setErr}
            refresh={(next) => setPolls(next)}
            room={room}
          />
        ) : (
          <QnaTab
            questions={questions}
            meetingId={meetingId}
            isModerator={isModerator}
            me={me}
            myName={myName ?? "Guest"}
            setErr={setErr}
            refresh={(next) => setQuestions(next)}
            room={room}
          />
        )}
      </div>
    </aside>
  );
}

function PollsTab({
  polls,
  meetingId,
  isModerator,
  me,
  setErr,
  refresh,
  room,
}: {
  polls: PollDTO[];
  meetingId: string | null;
  isModerator: boolean;
  me: string;
  setErr: (e: string | null) => void;
  refresh: (p: PollDTO[]) => void;
  room: ReturnType<typeof useRoomContext>;
}) {
  const { t } = useTranslation();
  const [question, setQuestion] = useState("");
  const [options, setOptions] = useState<string[]>(["", ""]);

  async function create() {
    if (!meetingId) return;
    const opts = options.map((o) => o.trim()).filter(Boolean);
    if (!question.trim() || opts.length < 2) return;
    try {
      const p = await api.createPoll(meetingId, { question: question.trim(), options: opts });
      refresh([p, ...polls]);
      setQuestion("");
      setOptions(["", ""]);
      void broadcastPollsActivity(room, "poll");
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function vote(poll: PollDTO, idx: number) {
    try {
      const updated = await api.votePoll(poll.id, me, idx);
      refresh(polls.map((p) => (p.id === poll.id ? updated : p)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function close(poll: PollDTO) {
    try {
      const updated = await api.closePoll(poll.id);
      refresh(polls.map((p) => (p.id === poll.id ? updated : p)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  return (
    <>
      {isModerator && (
        <details className="rounded border border-primary-700 p-2">
          <summary className="cursor-pointer text-sm text-slate-200 font-medium">
            {t("polls.createTitle", { defaultValue: "Create a poll" })}
          </summary>
          <div className="mt-2 space-y-2">
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={t("polls.questionPlaceholder", { defaultValue: "Your question" })}
              className="w-full px-2 py-1.5 rounded bg-primary-800 text-slate-100 border border-primary-700 text-sm"
            />
            {options.map((o, i) => (
              <input
                key={i}
                value={o}
                onChange={(e) => {
                  const next = [...options];
                  next[i] = e.target.value;
                  setOptions(next);
                }}
                placeholder={t("polls.optionPlaceholder", { defaultValue: "Option {{n}}", n: i + 1 })}
                className="w-full px-2 py-1.5 rounded bg-primary-800 text-slate-100 border border-primary-700 text-sm"
              />
            ))}
            <div className="flex items-center gap-2">
              {options.length < 6 && (
                <button
                  type="button"
                  onClick={() => setOptions([...options, ""])}
                  className="text-xs text-slate-300 hover:text-slate-100"
                >
                  {t("polls.addOption", { defaultValue: "+ Add option" })}
                </button>
              )}
              <button
                type="button"
                onClick={() => void create()}
                className="ml-auto px-2 py-1 rounded-md bg-accent-500 hover:bg-accent-600 text-white text-xs"
              >
                {t("polls.createButton", { defaultValue: "Create" })}
              </button>
            </div>
          </div>
        </details>
      )}
      {polls.length === 0 && (
        <p className="text-sm text-slate-400 px-1">
          {t("polls.empty", { defaultValue: "No polls yet." })}
        </p>
      )}
      {polls.map((p) => (
        <div key={p.id} className="rounded border border-primary-700 bg-primary-800/40 p-3 text-sm space-y-2">
          <div className="flex items-start justify-between gap-2">
            <p className="font-medium text-slate-100">{p.question}</p>
            <span className="text-[10px] uppercase tracking-wide text-slate-400">
              {p.status === "closed"
                ? t("polls.closed", { defaultValue: "closed" })
                : t("polls.open", { defaultValue: "open" })}
            </span>
          </div>
          <ul className="space-y-1">
            {p.options.map((opt, i) => {
              const total = p.total_votes || 1;
              const pct = Math.round((p.counts[i] / total) * 100);
              return (
                <li key={i}>
                  <button
                    type="button"
                    disabled={p.status !== "open"}
                    onClick={() => void vote(p, i)}
                    className="w-full text-left text-xs"
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-slate-200">{opt}</span>
                      <span className="text-slate-400">{p.counts[i]} ({pct}%)</span>
                    </div>
                    <div className="h-1.5 mt-1 rounded bg-primary-700 overflow-hidden">
                      <div
                        className="h-full bg-accent-500"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
          {isModerator && p.status === "open" && (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => void close(p)}
                className="text-xs text-amber-400 hover:text-amber-300"
              >
                {t("polls.closePoll", { defaultValue: "Close poll" })}
              </button>
            </div>
          )}
        </div>
      ))}
    </>
  );
}

function QnaTab({
  questions,
  meetingId,
  isModerator,
  me,
  myName,
  setErr,
  refresh,
  room,
}: {
  questions: QuestionDTO[];
  meetingId: string | null;
  isModerator: boolean;
  me: string;
  myName: string;
  setErr: (e: string | null) => void;
  refresh: (q: QuestionDTO[]) => void;
  room: ReturnType<typeof useRoomContext>;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");

  async function ask() {
    if (!meetingId || !text.trim()) return;
    try {
      const q = await api.askQuestion(meetingId, {
        asker_identity: me,
        asker_name: myName,
        question: text.trim(),
      });
      refresh([q, ...questions]);
      setText("");
      void broadcastPollsActivity(room, "question");
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function up(q: QuestionDTO) {
    try {
      const updated = await api.upvoteQuestion(q.id, me);
      refresh(questions.map((x) => (x.id === q.id ? updated : x)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function answer(q: QuestionDTO) {
    try {
      const updated = await api.answerQuestion(q.id);
      refresh(questions.map((x) => (x.id === q.id ? updated : x)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function dismiss(q: QuestionDTO) {
    try {
      const updated = await api.dismissQuestion(q.id);
      refresh(questions.map((x) => (x.id === q.id ? updated : x)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const visible = questions.filter((q) => q.status !== "dismissed");
  return (
    <>
      <div className="flex flex-col gap-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          maxLength={600}
          placeholder={t("polls.qnaPlaceholder", { defaultValue: "Ask a question…" })}
          className="w-full px-2 py-1.5 rounded bg-primary-800 text-slate-100 border border-primary-700 text-sm resize-y"
        />
        <button
          type="button"
          onClick={() => void ask()}
          disabled={!text.trim()}
          className="self-end px-2 py-1 rounded-md bg-accent-500 hover:bg-accent-600 text-white text-xs disabled:opacity-50"
        >
          {t("polls.qnaAsk", { defaultValue: "Ask" })}
        </button>
      </div>
      {visible.length === 0 && (
        <p className="text-sm text-slate-400 px-1">
          {t("polls.qnaEmpty", { defaultValue: "No questions yet." })}
        </p>
      )}
      {visible
        .slice()
        .sort((a, b) => {
          // Answered last; among the rest sort by upvotes desc then time.
          if (a.status !== b.status) return a.status === "answered" ? 1 : -1;
          if (a.upvotes !== b.upvotes) return b.upvotes - a.upvotes;
          return (a.created_at ?? "").localeCompare(b.created_at ?? "");
        })
        .map((q) => (
          <div
            key={q.id}
            data-testid={`qna-${q.id}`}
            className={[
              "rounded border p-3 text-sm space-y-1.5",
              q.status === "answered"
                ? "border-accent-500/40 bg-accent-500/5"
                : "border-primary-700 bg-primary-800/40",
            ].join(" ")}
          >
            <p className="text-slate-100">{q.question}</p>
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span className="truncate">— {q.asker_name}</span>
              <button
                type="button"
                onClick={() => void up(q)}
                className="ml-auto inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-700 hover:bg-primary-600 text-slate-200"
                title={t("polls.qnaUpvote", { defaultValue: "Upvote" })}
                aria-label={t("polls.qnaUpvote", { defaultValue: "Upvote" })}
              >
                <ThumbsUp size={12} /> {q.upvotes}
              </button>
              {isModerator && q.status === "open" && (
                <>
                  <button
                    type="button"
                    onClick={() => void answer(q)}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-accent-500 hover:bg-accent-600 text-white"
                  >
                    <Check size={12} /> {t("polls.qnaMarkAnswered", { defaultValue: "Answered" })}
                  </button>
                  <button
                    type="button"
                    onClick={() => void dismiss(q)}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-700 hover:bg-red-700 text-slate-200"
                  >
                    <Trash2 size={12} />
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
    </>
  );
}
