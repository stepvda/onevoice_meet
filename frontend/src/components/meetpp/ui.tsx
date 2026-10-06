import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { meetppApi } from "../../lib/meetpp/api";
import { usePreferences } from "../../lib/preferences";

/** Re-render every `ms` while `enabled`. */
export function useNow(ms = 1000, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms, enabled]);
  return now;
}

/** prefers-reduced-motion or the in-app accessibility preference. */
export function useReducedMotion(): boolean {
  const pref = usePreferences((s) => s.accessibility.reducedMotion);
  const [media, setMedia] = useState(() =>
    typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)").matches : false,
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setMedia(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return pref || media;
}

export function useElementWidth(ref: RefObject<HTMLElement>, fallback = 1280): number {
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.getBoundingClientRect().width || fallback);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0]?.contentRect;
      if (cr) setW(cr.width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, fallback]);
  return w;
}

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Minutes markdown: headings, lists, tables and `> **RESOLVED:**` blocks. */
export function MdView({ text, compact }: { text: string; compact?: boolean }) {
  return (
    <div
      className={cx(
        "meetpp-md text-slate-800",
        compact ? "text-[13px]" : "text-sm",
        "[&_h1]:mb-1 [&_h1]:mt-2 [&_h1]:text-base [&_h1]:font-bold",
        "[&_h2]:mb-1 [&_h2]:mt-2 [&_h2]:text-[15px] [&_h2]:font-bold",
        "[&_h3]:mb-1 [&_h3]:mt-2 [&_h3]:font-semibold [&_h3]:text-blue-900",
        "[&_p]:my-1 [&_p]:leading-relaxed [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5",
        "[&_blockquote]:my-2 [&_blockquote]:border-l-4 [&_blockquote]:border-slate-800 [&_blockquote]:bg-slate-50 [&_blockquote]:px-3 [&_blockquote]:py-1",
        "[&_table]:my-2 [&_table]:w-full [&_table]:border-collapse [&_td]:border [&_td]:border-slate-200 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-slate-200 [&_th]:bg-slate-50 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left",
        "[&_a]:text-blue-700 [&_a]:underline",
      )}
    >
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </div>
  );
}

const blobCache = new Map<string, Promise<string>>();

/** <img> for an API path that needs the room token / JWT headers. */
export function AuthImage({ url, alt, className }: { url: string; alt: string; className?: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    let p = blobCache.get(url);
    if (!p) {
      p = meetppApi.attachmentBlob(url).then((b) => URL.createObjectURL(b));
      blobCache.set(url, p);
      p.catch(() => blobCache.delete(url));
    }
    p.then((s) => alive && setSrc(s)).catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [url]);
  if (failed) return <div className={cx("grid place-items-center bg-slate-100 text-xs text-slate-400", className)}>{alt}</div>;
  if (!src) return <div className={cx("animate-pulse bg-slate-100", className)} />;
  return <img src={src} alt={alt} className={className} />;
}

export function Pill({ children, tone = "slate", title }: { children: ReactNode; tone?: Tone; title?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium", TONES[tone])}>
      {children}
    </span>
  );
}

export type Tone = "slate" | "green" | "blue" | "amber" | "red" | "indigo" | "purple";

const TONES: Record<Tone, string> = {
  slate: "bg-slate-100 text-slate-600",
  green: "bg-emerald-100 text-emerald-800",
  blue: "bg-blue-100 text-blue-800",
  amber: "bg-amber-100 text-amber-900",
  red: "bg-rose-100 text-rose-700",
  indigo: "bg-indigo-100 text-indigo-800",
  purple: "bg-purple-100 text-purple-800",
};

/** Small inline text editor used by item cards (Enter saves, Esc cancels). */
export function InlineInput({
  value,
  onSave,
  onCancel,
  multiline,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onSave: (v: string) => void;
  onCancel: () => void;
  multiline?: boolean;
  placeholder?: string;
  ariaLabel: string;
}) {
  const [v, setV] = useState(value);
  const ref = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  const common = {
    ref,
    value: v,
    placeholder,
    "aria-label": ariaLabel,
    onChange: (e: { target: { value: string } }) => setV(e.target.value),
    onKeyDown: (e: ReactKeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      } else if (e.key === "Enter" && (!multiline || e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        onSave(v);
      }
    },
    className: "w-full rounded border border-blue-300 bg-white px-2 py-1 text-sm text-slate-800 outline-none focus:ring-2 focus:ring-blue-400",
  };
  return multiline ? <textarea rows={4} {...common} /> : <input {...common} />;
}

export function useOnClickOutside(ref: RefObject<HTMLElement>, onOutside: () => void, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onOutside();
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [ref, onOutside, enabled]);
}
