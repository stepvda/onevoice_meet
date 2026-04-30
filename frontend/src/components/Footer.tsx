import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

/**
 * Global footer rendered at the bottom of every non-meeting / non-recorder
 * route. Carries the legally-required links (Terms, Privacy, Legal) plus
 * the operator credit. Excluded from the in-meeting view by MainArea so it
 * doesn't appear in recordings or distract during a call.
 */
export default function Footer() {
  const { t } = useTranslation();
  const year = new Date().getFullYear();
  return (
    <footer
      data-testid="site-footer"
      className="mt-auto border-t border-primary-700 px-4 lg:px-8 py-4 text-xs text-slate-400"
    >
      <div className="max-w-4xl mx-auto flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
        <div>
          © {year} TI One Voice ·{" "}
          <a href="https://one.witysk.org" className="hover:text-accent-500 hover:underline">
            one.witysk.org
          </a>
        </div>
        <nav className="flex items-center gap-4">
          <Link to="/terms" className="hover:text-accent-500 hover:underline" data-testid="footer-terms">
            {t("footer.terms", { defaultValue: "Terms" })}
          </Link>
          <Link to="/privacy" className="hover:text-accent-500 hover:underline" data-testid="footer-privacy">
            {t("footer.privacy", { defaultValue: "Privacy" })}
          </Link>
          <Link to="/legal" className="hover:text-accent-500 hover:underline" data-testid="footer-legal">
            {t("footer.legal", { defaultValue: "Legal notice" })}
          </Link>
        </nav>
      </div>
    </footer>
  );
}
