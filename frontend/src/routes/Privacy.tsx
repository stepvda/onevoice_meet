/**
 * Privacy Statement — GDPR-aligned. Written for a Belgium-based operator
 * since that's the jurisdiction the user specified. Not lawyer-reviewed;
 * intended as a credible MVP that covers the essentials of Articles 13/14.
 */
const EFFECTIVE_DATE = "2026-04-30";
const CONTACT_EMAIL = "stephane@stepvda.com";

export default function Privacy() {
  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="privacy-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-2">Privacy Statement</h1>
      <p className="text-sm text-slate-500 mb-6">Effective date: {EFFECTIVE_DATE}</p>

      <Section title="1. Who we are">
        <p>
          meet.witysk.org is operated by TI One Voice in Belgium. We are the
          "controller" of personal data processed through this Service in the
          meaning of Regulation (EU) 2016/679 (GDPR). Contact:{" "}
          <a href={`mailto:${CONTACT_EMAIL}`} className="text-accent-500 hover:underline">
            {CONTACT_EMAIL}
          </a>
          .
        </p>
      </Section>

      <Section title="2. What we collect and why">
        <p>We process the following categories of personal data:</p>
        <ul className="list-disc list-inside space-y-1">
          <li>
            <b>Account data</b> (email, username, hashed password, optional display
            name, optional profile picture) — to identify you and let you sign in.
            Legal basis: performance of the contract (Art. 6(1)(b) GDPR).
          </li>
          <li>
            <b>Meeting metadata</b> (room names you create, scheduled times, your
            participants' chosen display names, join/leave timestamps, host audit
            log) — to operate the meeting service. Legal basis: contract performance.
          </li>
          <li>
            <b>Chat messages and uploaded images</b> in meeting chat — to deliver
            the chat feature and let late joiners read history. Legal basis:
            contract performance. Retention: until the host deletes the message or
            the meeting is permanently removed.
          </li>
          <li>
            <b>Recordings</b> (when a host starts recording) — to make a meeting
            playback available to that host. Legal basis: contract performance with
            the host. Retention: up to 30 days, then automatic deletion.
          </li>
          <li>
            <b>IP address and User-Agent</b> for connecting clients — to operate the
            Service securely (rate-limiting, abuse prevention) and to enable WebRTC
            transport. Legal basis: legitimate interest (Art. 6(1)(f)) in keeping
            the Service available to other users. Retention: 30 days in connection
            logs.
          </li>
          <li>
            <b>Payment metadata</b> (PayPal subscription / order id, plan, expiry)
            — to grant or revoke meeting-creation rights. We do <b>not</b> see or
            store your card / bank details; PayPal handles the payment instrument.
            Legal basis: contract performance.
          </li>
        </ul>
        <p>
          We do <b>not</b> use cookies for analytics or advertising. We do <b>not</b>{" "}
          run third-party trackers. The only persistent storage we set in your
          browser is the access token and a few UI preferences.
        </p>
      </Section>

      <Section title="3. Where data is stored">
        <p>
          The Service runs on servers located in the European Union (Hetzner Cloud,
          Germany). Personal data does not leave the EU/EEA except to the extent
          necessary for PayPal payment processing, which involves transfer to PayPal
          (Europe) S.à.r.l. et Cie, S.C.A. (Luxembourg). PayPal is a separate data
          controller for payment information.
        </p>
      </Section>

      <Section title="4. Sharing with third parties">
        <p>We share personal data only with:</p>
        <ul className="list-disc list-inside space-y-1">
          <li>
            <b>PayPal</b> — when you initiate or maintain a paid subscription, to
            charge your account and reconcile entitlement.
          </li>
          <li>
            <b>Resend</b> — to deliver transactional emails (account-related notices,
            meeting invitations you ask us to send).
          </li>
          <li>
            <b>YouTube</b> — only when a meeting host explicitly clicks "Publish to
            YouTube", and only the recording in question.
          </li>
          <li>
            <b>Hosting and infrastructure providers</b> as data processors under EU
            standard contractual clauses (Hetzner; LiveKit Cloud is not used —
            LiveKit runs self-hosted on our server).
          </li>
        </ul>
        <p>
          We never sell personal data and we never share it with advertising
          networks.
        </p>
      </Section>

      <Section title="5. Your rights">
        <p>You have the right to:</p>
        <ul className="list-disc list-inside space-y-1">
          <li>Access the personal data we hold about you.</li>
          <li>Correct inaccurate data (via the Account page or by writing to us).</li>
          <li>Delete your account and associated personal data (right to erasure).</li>
          <li>Object to processing based on legitimate interest.</li>
          <li>Receive your data in a portable machine-readable format.</li>
          <li>Withdraw consent at any time, where consent was the legal basis.</li>
          <li>
            Lodge a complaint with the Belgian Data Protection Authority (Gegevensbeschermingsautoriteit
            / Autorité de protection des données) — contact@apd-gba.be —
            without prejudice to any other remedy.
          </li>
        </ul>
        <p>
          To exercise these rights, email{" "}
          <a href={`mailto:${CONTACT_EMAIL}`} className="text-accent-500 hover:underline">
            {CONTACT_EMAIL}
          </a>
          . We respond within 30 days.
        </p>
      </Section>

      <Section title="6. Retention">
        <p>
          Account and profile data: kept while the account is active, deleted within
          30 days of account closure. Recordings: 30 days. Chat messages: until the
          host deletes the meeting. Logs: 30 days. Invoicing records (where
          applicable to paid subscriptions): retained for 7 years as required by
          Belgian tax law.
        </p>
      </Section>

      <Section title="7. Security">
        <p>
          We use HTTPS for all traffic, Argon2 for password hashing, signed JWTs for
          session tokens, and regular dependency updates. No system is perfectly
          secure; in the event of a personal data breach affecting you, we will
          notify you and the supervisory authority within the timeframes set by Art.
          33–34 GDPR.
        </p>
      </Section>

      <Section title="8. Children">
        <p>
          The Service is not directed to children under 16. If we learn we have
          collected data from a child under 16 without parental consent, we will
          delete it.
        </p>
      </Section>

      <Section title="9. Changes to this statement">
        <p>
          We may update this Privacy Statement; the effective date at the top tells
          you when the current version was published. Substantive changes are
          announced on the home page.
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
