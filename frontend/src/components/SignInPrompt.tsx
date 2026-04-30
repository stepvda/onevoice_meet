/**
 * Friendly "you need to sign in on one.witysk.org" card. Used by routes that
 * 401 against an upstream API when the user is not authenticated (Café,
 * Recordings, …). Same look everywhere; pages supply their own headline icon
 * and copy.
 */
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LogIn, UserPlus, type LucideIcon } from "lucide-react";
import { Card } from "./ui";
import { startSsoRedirect } from "../lib/auth";

interface Props {
  icon: LucideIcon;
  title: string;
  body: string;
  testId?: string;
}

export default function SignInPrompt({ icon: Icon, title, body, testId }: Props) {
  const { t } = useTranslation();
  return (
    <Card data-testid={testId}>
      <div className="flex flex-col items-center text-center gap-4 py-6">
        <div className="h-14 w-14 rounded-full bg-accent-500/15 border border-accent-500/40 flex items-center justify-center">
          <Icon size={26} className="text-accent-500" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-slate-100">{title}</h2>
          <p className="text-sm text-slate-400 mt-1 max-w-md">{body}</p>
        </div>
        <button
          type="button"
          onClick={() => startSsoRedirect()}
          data-testid="signin-prompt-button"
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white text-sm font-semibold"
        >
          <LogIn size={16} /> {t("signInPrompt.button")}
        </button>
        <div className="text-xs text-slate-500 flex items-center gap-3">
          <Link
            to="/signup"
            data-testid="signin-prompt-signup"
            className="inline-flex items-center gap-1 text-accent-500 hover:underline"
          >
            <UserPlus size={12} />
            {t("signInPrompt.signup", { defaultValue: "Create a meet account" })}
          </Link>
          <span>·</span>
          <Link
            to="/login"
            data-testid="signin-prompt-login"
            className="text-accent-500 hover:underline"
          >
            {t("signInPrompt.login", { defaultValue: "Sign in with email" })}
          </Link>
        </div>
      </div>
    </Card>
  );
}
