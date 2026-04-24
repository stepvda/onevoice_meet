/**
 * Shared UI primitives — matches one.witysk.org's Button/Card/Input patterns.
 * Tailwind-first; no CSS-in-JS.
 */
import { ReactNode, forwardRef } from "react";

type Variant = "primary" | "secondary" | "danger" | "outline" | "ghost" | "accent";
type Size = "sm" | "md" | "lg";

const base =
  "inline-flex items-center justify-center gap-2 font-medium rounded-lg " +
  "transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 " +
  "focus:ring-offset-primary-900 disabled:opacity-50 disabled:cursor-not-allowed";

const sizes: Record<Size, string> = {
  sm: "px-3 py-1.5 text-sm",
  md: "px-4 py-2",
  lg: "px-6 py-3 text-lg",
};

const variants: Record<Variant, string> = {
  primary: "bg-primary-500 text-white hover:bg-primary-600 focus:ring-primary-500",
  secondary:
    "bg-primary-800 text-slate-100 hover:bg-primary-700 focus:ring-primary-400",
  accent: "bg-accent-500 text-white hover:bg-accent-600 focus:ring-accent-500",
  danger: "bg-red-600 text-white hover:bg-red-700 focus:ring-red-500",
  outline:
    "border-2 border-primary-400 text-primary-100 hover:bg-primary-700 focus:ring-primary-400",
  ghost: "text-slate-200 hover:bg-primary-800 focus:ring-primary-400",
};

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", className = "", ...rest },
  ref
) {
  return (
    <button
      ref={ref}
      className={`${base} ${sizes[size]} ${variants[variant]} ${className}`}
      {...rest}
    />
  );
});

export function Card({
  className = "",
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement> & { children: ReactNode }) {
  return (
    <div
      className={`bg-primary-800/70 backdrop-blur border border-primary-700 rounded-lg shadow-lg p-6 ${className}`}
      {...rest}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  subtitle,
  right,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-4 gap-4">
      <div>
        <h2 className="text-lg font-semibold text-slate-50">{title}</h2>
        {subtitle && <p className="text-sm text-slate-300 mt-1">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

export function Label({
  htmlFor,
  children,
  className = "",
}: {
  htmlFor: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label
      htmlFor={htmlFor}
      className={`block text-sm font-medium text-slate-200 mb-1 ${className}`}
    >
      {children}
    </label>
  );
}

type InputProps = React.InputHTMLAttributes<HTMLInputElement> & {
  invalid?: boolean;
};

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid = false, className = "", ...rest },
  ref
) {
  return (
    <input
      ref={ref}
      className={[
        "w-full px-3 py-2 rounded-lg shadow-sm",
        "bg-primary-900/60 text-slate-100 placeholder:text-slate-400",
        "border",
        invalid ? "border-red-500" : "border-primary-700",
        "focus:outline-none focus:ring-2 focus:border-primary-400",
        invalid ? "focus:ring-red-500" : "focus:ring-primary-500",
        "disabled:bg-primary-900/30 disabled:cursor-not-allowed",
        className,
      ].join(" ")}
      {...rest}
    />
  );
});

type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { className = "", children, ...rest },
  ref
) {
  return (
    <select
      ref={ref}
      className={[
        "w-full px-3 py-2 rounded-lg shadow-sm",
        "bg-primary-900/60 text-slate-100",
        "border border-primary-700",
        "focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-400",
        className,
      ].join(" ")}
      {...rest}
    >
      {children}
    </select>
  );
});

export function Toggle({
  id,
  label,
  checked,
  onChange,
  description,
}: {
  id: string;
  label: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  description?: ReactNode;
}) {
  return (
    <div className="flex items-start gap-3">
      <button
        type="button"
        role="switch"
        id={id}
        data-testid={id}
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={[
          "relative inline-flex h-6 w-11 flex-shrink-0 rounded-full transition-colors",
          "focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-primary-900 focus:ring-primary-500",
          checked ? "bg-accent-500" : "bg-primary-700",
        ].join(" ")}
      >
        <span
          className={[
            "pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition mt-0.5",
            checked ? "translate-x-5" : "translate-x-0.5",
          ].join(" ")}
        />
      </button>
      <div className="flex-1">
        <label htmlFor={id} className="text-sm font-medium text-slate-100 cursor-pointer">
          {label}
        </label>
        {description && <p className="text-xs text-slate-400 mt-0.5">{description}</p>}
      </div>
    </div>
  );
}

export function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p className="text-xs text-slate-400 mt-1">{hint}</p>}
    </div>
  );
}
