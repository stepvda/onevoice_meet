import { useMemo } from "react";
import { Check, X } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * Live password complexity checker — same rules + visuals as one.witysk.org's
 * (4 boolean checks → 0..4 strength, with a colored 4-segment bar and a
 * checklist beneath). Renders nothing for an empty password so it doesn't
 * scream at the user before they've typed anything.
 */
const RULES = [
  { key: "length", labelKey: "passwordRules.length", defaultLabel: "At least 8 characters", test: (pw: string) => pw.length >= 8 },
  { key: "uppercase", labelKey: "passwordRules.uppercase", defaultLabel: "One uppercase letter", test: (pw: string) => /[A-Z]/.test(pw) },
  { key: "lowercase", labelKey: "passwordRules.lowercase", defaultLabel: "One lowercase letter", test: (pw: string) => /[a-z]/.test(pw) },
  { key: "digit", labelKey: "passwordRules.digit", defaultLabel: "One digit", test: (pw: string) => /\d/.test(pw) },
] as const;

const TIER = {
  weak: { bar: "bg-red-500", text: "text-red-400", labelKey: "passwordStrength.weak", defaultLabel: "Weak" },
  fair: { bar: "bg-orange-400", text: "text-orange-300", labelKey: "passwordStrength.fair", defaultLabel: "Fair" },
  good: { bar: "bg-yellow-400", text: "text-yellow-300", labelKey: "passwordStrength.good", defaultLabel: "Good" },
  strong: { bar: "bg-accent-500", text: "text-accent-500", labelKey: "passwordStrength.strong", defaultLabel: "Strong" },
} as const;

export default function PasswordStrengthIndicator({ password }: { password: string }) {
  const { t } = useTranslation();
  const results = useMemo(
    () => RULES.map((rule) => ({ ...rule, passed: rule.test(password) })),
    [password]
  );

  const passedCount = results.filter((r) => r.passed).length;
  const tier =
    passedCount === 0
      ? null
      : passedCount <= 1
      ? TIER.weak
      : passedCount <= 2
      ? TIER.fair
      : passedCount <= 3
      ? TIER.good
      : TIER.strong;

  if (!password) return null;

  return (
    <div className="mt-2 space-y-2" data-testid="password-strength">
      <div className="flex items-center gap-2">
        <div className="flex-1 flex gap-1">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className={[
                "h-1.5 flex-1 rounded-full transition-colors",
                i < passedCount && tier ? tier.bar : "bg-primary-700",
              ].join(" ")}
            />
          ))}
        </div>
        {tier && (
          <span className={`text-xs font-medium ${tier.text}`} data-testid="password-strength-label">
            {t(tier.labelKey, { defaultValue: tier.defaultLabel })}
          </span>
        )}
      </div>

      <ul className="space-y-1">
        {results.map((rule) => (
          <li key={rule.key} className="flex items-center gap-1.5 text-xs">
            {rule.passed ? (
              <Check size={12} className="text-accent-500 shrink-0" />
            ) : (
              <X size={12} className="text-slate-500 shrink-0" />
            )}
            <span className={rule.passed ? "text-accent-500" : "text-slate-400"}>
              {t(rule.labelKey, { defaultValue: rule.defaultLabel })}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
