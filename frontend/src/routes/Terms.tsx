/**
 * Terms of Service. Plain-text legalese kept inside the SPA so it loads
 * without a backend round-trip and is part of the same i18n / styling
 * system as the rest of the app.
 *
 * Effective date is set at deploy time via the build but the date string
 * in the page is the *content* effective date — bump it when the policy
 * actually changes substantively.
 */
import { Link } from "react-router-dom";

const EFFECTIVE_DATE = "2026-04-30";
const CONTACT_EMAIL = "stephane@stepvda.com";

export default function Terms() {
  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="terms-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-2">Terms of Service</h1>
      <p className="text-sm text-slate-500 mb-6">Effective date: {EFFECTIVE_DATE}</p>

      <Section title="1. About this service">
        <p>
          meet.witysk.org (the "Service") is a browser-based video-conferencing tool
          operated by TI One Voice for the witysk.org community. By creating an
          account or joining a meeting you agree to these Terms.
        </p>
      </Section>

      <Section title="2. Accounts">
        <p>
          You may create an account by signing up with an email address and password,
          or by signing in via your existing one.witysk.org account. You are responsible
          for keeping your credentials confidential and for all activity on your
          account. We may suspend or remove accounts that violate these Terms or
          applicable law.
        </p>
      </Section>

      <Section title="3. Meeting creation: free trial, vouchers, subscriptions">
        <p>
          Native accounts receive a one-time 10-day free trial during which they may
          create meetings without payment. After the trial expires, meeting creation
          is restricted to users who hold a valid voucher entitlement, an active
          PayPal subscription (€2 / month) or a paid annual access (€20 / year, paid
          in advance). Joining meetings, using audio Café, chat and recording playback
          remain available to every signed-in user regardless of paid status. Users
          authenticated via one.witysk.org SSO have meeting-creation rights at no
          additional cost.
        </p>
        <p>
          Subscriptions auto-renew unless cancelled in your PayPal account before the
          renewal date. Annual access does not auto-renew. We do not pro-rate refunds
          for unused time on subscriptions or vouchers, but you can request a goodwill
          refund within 14 days of payment by emailing us — see clause 11.
        </p>
      </Section>

      <Section title="4. Acceptable use">
        <p>
          You agree not to use the Service to: harass or threaten other participants;
          share illegal content; impersonate another person; share your account with
          others; circumvent the meeting-creation gating; record or redistribute a
          meeting without the consent of its participants; or interfere with the
          Service's infrastructure (denial-of-service, scraping, abuse of the API
          rate limits).
        </p>
      </Section>

      <Section title="5. Content responsibility">
        <p>
          Meeting hosts are responsible for the conduct and content of their
          meetings. We do not pre-moderate live audio/video content. Chat messages
          and uploaded images are stored on our servers; we may remove content that
          violates these Terms or that is reported as illegal.
        </p>
      </Section>

      <Section title="6. Recordings">
        <p>
          A meeting host may record their meeting. Recordings are stored on our
          servers for up to 30 days and are accessible only to the host. Hosts must
          inform participants when a recording starts. Local laws (notably the
          Belgian Royal Decree of 1968 on telecommunication confidentiality) may
          require explicit consent of every participant; the host is responsible
          for obtaining it.
        </p>
      </Section>

      <Section title="7. Data and privacy">
        <p>
          Our handling of personal data is described in the{" "}
          <Link to="/privacy" className="text-accent-500 hover:underline">
            Privacy Statement
          </Link>
          .
        </p>
      </Section>

      <Section title="8. Availability and changes">
        <p>
          The Service is provided on a best-effort basis with no uptime guarantee.
          We may add, modify or remove features, change pricing, or discontinue the
          Service with reasonable notice. Substantive changes to these Terms will be
          announced on the home page; continued use of the Service after the change
          takes effect constitutes acceptance.
        </p>
      </Section>

      <Section title="9. Disclaimer of warranties">
        <p>
          The Service is provided "as is" without warranties of any kind. We do not
          warrant that the Service will be uninterrupted, error-free, or fit for any
          particular purpose. To the maximum extent permitted by Belgian law, our
          aggregate liability to you is capped at the amount you paid us in the 12
          months preceding the claim, or €50, whichever is greater.
        </p>
      </Section>

      <Section title="10. Termination">
        <p>
          You may close your account at any time from the Account page. We may
          terminate accounts that breach these Terms, without refund for time
          already paid. Upon termination we delete your personal data within 30 days
          except where retention is required by law (e.g. invoicing records).
        </p>
      </Section>

      <Section title="11. Contact">
        <p>
          Questions, refund requests, or notices under these Terms:{" "}
          <a href={`mailto:${CONTACT_EMAIL}`} className="text-accent-500 hover:underline">
            {CONTACT_EMAIL}
          </a>
          .
        </p>
      </Section>

      <Section title="12. Governing law">
        <p>
          These Terms are governed by the laws of Belgium. Disputes that cannot be
          resolved amicably fall under the exclusive jurisdiction of the courts of
          the judicial district of the operator's residence.
        </p>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="text-lg font-semibold text-slate-100 mb-2">{title}</h2>
      <div className="text-sm text-slate-300 leading-relaxed flex flex-col gap-2">{children}</div>
    </section>
  );
}
