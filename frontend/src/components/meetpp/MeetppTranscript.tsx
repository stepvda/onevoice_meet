import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, Search, X } from "lucide-react";
import { useMeetpp, selectItem, showItem } from "../../lib/meetpp/store";
import {
  displayText,
  findMatches,
  fmtClock,
  groupTranscript,
  normalise,
  type TranscriptBlock,
} from "../../lib/meetpp/state";
import type { SegmentDto } from "../../lib/meetpp/types";
import { cx, useReducedMotion } from "./ui";

/**
 * Live transcript panel (FDD §5.6): virtualised, sticks to the newest line
 * while scrolled to the bottom, "↓ live (n new)" pill when scrolled up,
 * tier-1 grey lines replaced in place by tier-2 text, same-speaker lines
 * within 4 s merged, "● Name is speaking…", search, evidence highlight and
 * gap markers. Scrolling here is NOT a board interaction (no follow pause).
 */

const OVERSCAN_PX = 600;
const BOTTOM_SLACK_PX = 40;

function estimate(b: TranscriptBlock): number {
  if (b.kind === "gap") return 34;
  const chars = b.segments.reduce((n, s) => n + displayText(s).length, 0);
  return 30 + 19 * Math.max(1, Math.ceil(chars / 42));
}

interface Props {
  variant?: "live" | "review";
  className?: string;
  /** Hide the search box (egress). */
  noSearch?: boolean;
}

export default function MeetppTranscript({ variant = "live", className, noSearch }: Props) {
  const { t } = useTranslation();
  const segments = useMeetpp((s) => s.transcript);
  const ready = useMeetpp((s) => s.transcriptReady);
  const speaking = useMeetpp((s) => s.speaking);
  const tier2 = useMeetpp((s) => s.agent?.tier2 ?? s.snap?.session.agent?.tier2 ?? "off");
  const selected = useMeetpp((s) => s.selected);
  const jump = useMeetpp((s) => s.transcriptJump);
  const reduced = useReducedMotion();
  const live = variant === "live";

  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const q = query.trim();
  const filtered = useMemo(() => {
    if (!q) return segments;
    const nq = normalise(q);
    return segments.filter((s) => !s.is_gap && normalise(displayText(s)).includes(nq));
  }, [segments, q]);
  const blocks = useMemo(() => groupTranscript(filtered), [filtered]);

  const evidence = useMemo(() => new Set(selected?.evidence ?? []), [selected]);

  // ── virtualisation ──
  const scrollRef = useRef<HTMLDivElement>(null);
  const heights = useRef(new Map<string, number>());
  const keyIndex = useRef(new Map<string, number>());
  const [rev, setRev] = useState(0);
  const [vp, setVp] = useState({ top: 0, height: 600 });
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const leftAtCount = useRef(0);
  const [flashSeq, setFlashSeq] = useState<number | null>(null);
  const [citeSeq, setCiteSeq] = useState<number | null>(null);

  const offsets = useMemo(() => {
    const out = new Array<number>(blocks.length + 1);
    out[0] = 0;
    keyIndex.current = new Map();
    for (let i = 0; i < blocks.length; i++) {
      keyIndex.current.set(blocks[i].key, i);
      out[i + 1] = out[i] + (heights.current.get(blocks[i].key) ?? estimate(blocks[i]));
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocks, rev]);
  const total = offsets[blocks.length];
  const offsetsRef = useRef(offsets);
  offsetsRef.current = offsets;

  const range = useMemo(() => {
    const lo = Math.max(0, vp.top - OVERSCAN_PX);
    const hi = vp.top + vp.height + OVERSCAN_PX;
    let start = 0;
    let a = 0;
    let b = blocks.length;
    while (a < b) {
      const m = (a + b) >> 1;
      if (offsets[m + 1] <= lo) a = m + 1;
      else b = m;
    }
    start = a;
    let end = start;
    while (end < blocks.length && offsets[end] < hi) end++;
    return [start, end] as const;
  }, [offsets, vp, blocks.length]);

  const rafRef = useRef<number | null>(null);
  const bump = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      setRev((r) => r + 1);
    });
  }, []);

  // One ResizeObserver for all rendered rows (created lazily so rows can
  // register in their layout effects on the first render).
  const roRef = useRef<ResizeObserver | null>(null);
  const getRO = useCallback((): ResizeObserver | null => {
    if (roRef.current || typeof ResizeObserver === "undefined") return roRef.current;
    roRef.current = new ResizeObserver((entries) => {
      const el = scrollRef.current;
      let changed = false;
      for (const e of entries) {
        const target = e.target as HTMLElement;
        const key = target.dataset.key;
        if (!key || !target.isConnected) continue;
        const h = Math.ceil(target.getBoundingClientRect().height);
        const prev = heights.current.get(key);
        if (prev === h) continue;
        heights.current.set(key, h);
        changed = true;
        // Scroll anchoring: a row above the viewport changed height (e.g. a
        // tier-2 replacement) — keep the visible text where it is.
        if (el && prev !== undefined && !atBottomRef.current) {
          const idx = keyIndex.current.get(key);
          if (idx !== undefined && offsetsRef.current[idx] + prev <= el.scrollTop) el.scrollTop += h - prev;
        }
      }
      if (changed) bump();
    });
    return roRef.current;
  }, [bump]);
  useEffect(() => () => roRef.current?.disconnect(), []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setVp({ top: el.scrollTop, height: el.clientHeight });
    const bottom = el.scrollTop + el.clientHeight >= el.scrollHeight - BOTTOM_SLACK_PX;
    if (bottom !== atBottomRef.current) {
      atBottomRef.current = bottom;
      setAtBottom(bottom);
      if (!bottom) leftAtCount.current = segments.length;
    }
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    setVp({ top: el.scrollTop, height: el.clientHeight });
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setVp({ top: el.scrollTop, height: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Stick to the newest line while at the bottom (live, no search).
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !live || q) return;
    if (atBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [total, live, q]);

  const toLive = useCallback(() => {
    const el = scrollRef.current;
    setQuery("");
    atBottomRef.current = true;
    setAtBottom(true);
    if (el) requestAnimationFrame(() => (el.scrollTop = el.scrollHeight));
  }, []);

  // Evidence jump (↗ on an item).
  const pendingJump = useRef<number | null>(null);
  useEffect(() => {
    if (!jump) return;
    pendingJump.current = jump.seq;
    if (q) setQuery("");
    else setRev((r) => r + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump]);
  useEffect(() => {
    const seq = pendingJump.current;
    const el = scrollRef.current;
    if (seq === null || !el) return;
    const idx = blocks.findIndex((b) => b.segments.some((s) => s.seq === seq));
    if (idx < 0) return;
    pendingJump.current = null;
    atBottomRef.current = false;
    setAtBottom(false);
    leftAtCount.current = segments.length;
    el.scrollTo({ top: Math.max(0, offsets[idx] - el.clientHeight / 3), behavior: reduced ? "auto" : "smooth" });
    setFlashSeq(seq);
  }, [blocks, offsets, segments.length, reduced]);
  // Own effect: the jump effect re-runs (rows measured, new captions) and
  // would cancel the timer, leaving the block highlighted.
  useEffect(() => {
    if (flashSeq === null) return;
    const tm = window.setTimeout(() => setFlashSeq(null), 2500);
    return () => window.clearTimeout(tm);
  }, [flashSeq]);

  const newCount = atBottom ? 0 : Math.max(0, segments.length - leftAtCount.current);
  const speakingNames = speaking.map((s) => s.name).filter(Boolean);
  const visible = blocks.slice(range[0], range[1]);

  return (
    <section className={cx("flex h-full min-h-0 flex-col bg-white", className)} aria-label={t("meetpp.transcript.title", { defaultValue: "Live transcript" })}>
      <div className="flex items-center gap-2 border-b border-slate-200 px-3 py-2">
        <span className="text-[11px] font-bold uppercase tracking-wide text-slate-600">
          {live
            ? t("meetpp.transcript.title", { defaultValue: "Live transcript" })
            : t("meetpp.transcript.titleReview", { defaultValue: "Transcript" })}
        </span>
        {live && tier2 !== "up" && (
          <span className="text-[10px] text-slate-400" title={t("meetpp.transcript.tier1Hint", { defaultValue: "Refinement on the Mac Studio is unavailable; live text is used." })}>
            {t("meetpp.transcript.tier1", { defaultValue: "live-quality transcript" })}
          </span>
        )}
        <span className="flex-1" />
        {!noSearch && (
          <button
            type="button"
            className="rounded p-1 text-slate-500 hover:bg-slate-100"
            onClick={() => {
              setSearchOpen((v) => !v);
              if (searchOpen) setQuery("");
            }}
            aria-label={t("meetpp.transcript.search", { defaultValue: "Search transcript" })}
            aria-expanded={searchOpen}
          >
            <Search size={14} />
          </button>
        )}
        {live && !atBottom && (
          <button type="button" onClick={toLive} className="text-[11px] font-medium text-blue-700 hover:underline">
            {t("meetpp.transcript.toLive", { defaultValue: "↓ live" })}
          </button>
        )}
      </div>
      {searchOpen && !noSearch && (
        <div className="flex items-center gap-1 border-b border-slate-100 px-2 py-1.5">
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                setQuery("");
                setSearchOpen(false);
                toLive();
              }
            }}
            placeholder={t("meetpp.transcript.searchPlaceholder", { defaultValue: "Search…" })}
            aria-label={t("meetpp.transcript.search", { defaultValue: "Search transcript" })}
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs text-slate-800"
          />
          {q && (
            <span className="text-[10px] text-slate-500">
              {t("meetpp.transcript.matches", { defaultValue: "{{n}} lines", n: filtered.length })}
            </span>
          )}
          {q && (
            <button type="button" className="rounded p-1 text-slate-500 hover:bg-slate-100" onClick={toLive} aria-label={t("meetpp.transcript.clear", { defaultValue: "Clear search" })}>
              <X size={12} />
            </button>
          )}
        </div>
      )}
      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} onScroll={onScroll} className="absolute inset-0 overflow-y-auto overscroll-contain" role="log" aria-live="off">
          {blocks.length === 0 ? (
            <div className="p-4 text-center text-xs text-slate-400">
              {!ready && segments.length === 0
                ? t("meetpp.transcript.loading", { defaultValue: "Loading transcript…" })
                : q
                  ? t("meetpp.transcript.noMatch", { defaultValue: "No matching lines." })
                  : t("meetpp.transcript.empty", { defaultValue: "The transcript appears here when people speak." })}
            </div>
          ) : (
            <div style={{ height: total, position: "relative" }}>
              {visible.map((b, i) => (
                <MeasuredRow key={b.key} rowKey={b.key} top={offsets[range[0] + i]} getRO={getRO}>
                  <Block
                    block={b}
                    query={q}
                    evidence={evidence}
                    flashSeq={flashSeq}
                    citeOpen={citeSeq !== null && b.segments.some((s) => s.seq === citeSeq) ? citeSeq : null}
                    onLineClick={(seq) => setCiteSeq((cur) => (cur === seq ? null : seq))}
                  />
                </MeasuredRow>
              ))}
            </div>
          )}
        </div>
        {live && !atBottom && (
          <button
            type="button"
            onClick={toLive}
            className="absolute bottom-2 left-1/2 inline-flex -translate-x-1/2 items-center gap-1 rounded-full bg-blue-700 px-3 py-1 text-xs font-semibold text-white shadow-lg hover:bg-blue-800"
          >
            <ArrowDown size={12} />
            {newCount > 0
              ? t("meetpp.transcript.liveNew", { defaultValue: "live ({{n}} new)", n: newCount })
              : t("meetpp.transcript.live", { defaultValue: "live" })}
          </button>
        )}
      </div>
      {live && speakingNames.length > 0 && (
        <div className="mx-2 mb-2 rounded bg-slate-100 px-2 py-1 text-xs italic text-slate-500">
          ●{" "}
          {speakingNames.length === 1
            ? t("meetpp.transcript.speaking", { defaultValue: "{{name}} is speaking…", name: speakingNames[0] })
            : t("meetpp.transcript.speakingMany", { defaultValue: "{{names}} are speaking…", names: speakingNames.slice(0, 3).join(", ") })}
        </div>
      )}
    </section>
  );
}

function MeasuredRow({ rowKey, top, getRO, children }: { rowKey: string; top: number; getRO: () => ResizeObserver | null; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    const ro = getRO();
    if (!el || !ro) return;
    ro.observe(el);
    return () => ro.unobserve(el);
  }, [getRO]);
  return (
    <div ref={ref} data-key={rowKey} style={{ position: "absolute", top, left: 0, right: 0 }}>
      {children}
    </div>
  );
}

const Block = memo(function Block({
  block,
  query,
  evidence,
  flashSeq,
  citeOpen,
  onLineClick,
}: {
  block: TranscriptBlock;
  query: string;
  evidence: Set<number>;
  flashSeq: number | null;
  citeOpen: number | null;
  onLineClick: (seq: number) => void;
}) {
  const { t } = useTranslation();
  if (block.kind === "gap") {
    const g = block.segments[0];
    return (
      <div className="px-3 py-2 text-center text-[11px] italic text-slate-400">
        {t("meetpp.transcript.gap", {
          defaultValue: "— transcription interrupted {{from}}–{{to}}{{who}} ({{reason}}) —",
          from: fmtClock(g.t_start),
          to: fmtClock(g.t_end),
          who: g.name ? ` · ${g.name}` : "",
          reason: g.gap_reason || t("meetpp.transcript.gapUnknown", { defaultValue: "no audio" }),
        })}
      </div>
    );
  }
  const cited = block.segments.some((s) => evidence.has(s.seq));
  const flash = flashSeq !== null && block.segments.some((s) => s.seq === flashSeq);
  return (
    <div className={cx("border-l-4 px-3 py-1.5", cited ? "border-amber-400 bg-amber-50" : "border-transparent", flash && "bg-amber-100")}>
      <div className="text-[11px] font-semibold text-blue-700">
        <span className="mr-1 font-mono font-normal text-slate-400">{fmtClock(block.tStart)}</span>
        {block.name || block.identity || "?"}
      </div>
      <div className="text-[13px] leading-snug">
        {block.segments.map((s, i) => (
          <Line key={s.seq} seg={s} query={query} cited={evidence.has(s.seq)} lead={i > 0} onClick={() => onLineClick(s.seq)} />
        ))}
      </div>
      {citeOpen !== null && <CitedBy seq={citeOpen} />}
    </div>
  );
});

function Line({ seg, query, cited, lead, onClick }: { seg: SegmentDto; query: string; cited: boolean; lead: boolean; onClick: () => void }) {
  const text = displayText(seg);
  const refined = seg.tier >= 2 || !!seg.text_refined;
  const parts = query ? highlight(text, query) : [text];
  return (
    <span
      role="button"
      tabIndex={-1}
      onClick={onClick}
      className={cx("cursor-pointer", refined ? "text-slate-900" : "text-slate-500", cited && "rounded bg-amber-100")}
    >
      {lead ? " " : ""}
      {parts}
    </span>
  );
}

function highlight(text: string, query: string) {
  const ranges = findMatches(text, query);
  if (!ranges.length) return [text];
  const out: Array<string | JSX.Element> = [];
  let at = 0;
  ranges.forEach(([a, b], i) => {
    if (a > at) out.push(text.slice(at, a));
    out.push(
      <mark key={i} className="rounded bg-yellow-200 px-0.5 text-slate-900">
        {text.slice(a, b)}
      </mark>,
    );
    at = b;
  });
  if (at < text.length) out.push(text.slice(at));
  return out;
}

/** "Cited by D-2 · A-7" under a clicked line. */
function CitedBy({ seq }: { seq: number }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  const items = useMemo(() => {
    if (!snap) return [];
    const out: Array<{ kind: "decision" | "action"; id: string; ref: string; evidence: number[] }> = [];
    for (const d of snap.decisions) if (d.evidence?.includes(seq)) out.push({ kind: "decision", id: d.id, ref: d.ref, evidence: d.evidence });
    for (const a of snap.actions) if (a.evidence?.includes(seq)) out.push({ kind: "action", id: a.id, ref: a.ref, evidence: a.evidence });
    return out;
  }, [snap, seq]);
  const notes = useMemo(() => (snap ? snap.minutes.filter((m) => m.notes.some((n) => n.evidence?.includes(seq))).length : 0), [snap, seq]);
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-slate-500">
      {items.length === 0 && notes === 0 ? (
        t("meetpp.transcript.citedNone", { defaultValue: "Not cited by any item." })
      ) : (
        <>
          {t("meetpp.transcript.citedBy", { defaultValue: "Cited by" })}
          {items.map((it) => (
            <button
              key={it.id}
              type="button"
              className="rounded bg-amber-100 px-1.5 font-semibold text-amber-900 hover:bg-amber-200"
              onClick={() => {
                selectItem({ kind: it.kind, id: it.id, evidence: it.evidence });
                showItem(it.kind, it.id);
              }}
            >
              {it.ref}
            </button>
          ))}
          {notes > 0 && <span>{t("meetpp.transcript.citedNotes", { defaultValue: "running notes" })}</span>}
        </>
      )}
    </div>
  );
}
