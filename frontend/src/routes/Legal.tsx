/**
 * Legal disclaimer + operator information. Belgian e-commerce law (Code de
 * droit économique, Livre VI / Boek VI) requires every commercial website
 * to publish certain identifying information ("mentions légales");
 * this page covers that plus a substantive content/liability disclaimer.
 */
const EFFECTIVE_DATE = "2026-04-30";
const CONTACT_EMAIL = "stephane@stepvda.com";

export default function Legal() {
  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="legal-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-2">Legal Notice & Disclaimer</h1>
      <p className="text-sm text-slate-500 mb-6">Effective date: {EFFECTIVE_DATE}</p>

      <Section title="Operator">
        <p>
          The Service (meet.witysk.org) is operated by TI One Voice, an initiative
          based in Belgium. For legal correspondence, refund requests, takedown
          notices and questions about these terms, contact:{" "}
          <a href={`mailto:${CONTACT_EMAIL}`} className="text-accent-500 hover:underline">
            {CONTACT_EMAIL}
          </a>
          .
        </p>
      </Section>

      <Section title="Hosting">
        <p>
          The Service is hosted on infrastructure operated by Hetzner Online GmbH,
          Industriestr. 25, 91710 Gunzenhausen, Germany.
        </p>
      </Section>

      <Section title="No professional advice">
        <p>
          The Service is a communication tool. Anything said or shown during a
          meeting, posted in the chat, or shared in a recording is the
          responsibility of the speaker / poster — not of the operator. We do not
          endorse, verify, or vouch for content created by users.
        </p>
        <p>
          Information exchanged via meet.witysk.org should not be taken as legal,
          medical, financial, or any other kind of professional advice. Always
          consult a qualified professional for advice specific to your situation.
        </p>
      </Section>

      <Section title="Content liability">
        <p>
          As a "hosting provider" within the meaning of Article XII.19 of the
          Belgian Code of Economic Law (Boek XII / Livre XII), we are not liable
          for the content stored or transmitted at the request of a user, provided
          we act expeditiously to remove illegal content once we have actual
          knowledge of it. Notices of illegal content can be sent to the contact
          address above; please describe the content and the URL clearly.
        </p>
      </Section>

      <Section title="Recording and consent">
        <p>
          Belgian law (notably Article 314bis of the Penal Code and the Royal Decree
          of 1968 on the confidentiality of telecommunications) requires explicit
          consent of every participant before a private conversation is recorded.
          Meeting hosts who choose to record are responsible for obtaining that
          consent and for informing participants. The Service displays a recording
          indicator while a recording is in progress, but this does not by itself
          constitute legal consent.
        </p>
      </Section>

      <Section title="Intellectual property">
        <p>
          The Service software, branding, and design elements are the property of
          their respective owners. Open-source components are used under the terms
          of their licences. Content uploaded or transmitted by users remains the
          property of the user; by using the Service, the user grants us a limited,
          non-exclusive licence to host and display that content for the purpose of
          delivering the Service to other meeting participants.
        </p>
      </Section>

      <Section title="Cookies and tracking">
        <p>
          We do not use third-party analytics, advertising, or tracking cookies.
          The browser-side storage we set is strictly necessary to operate the
          Service (your access token, your chosen language, mute preferences). No
          cookie banner is required for strictly-necessary storage under Belgian
          implementation of Directive 2002/58/EC.
        </p>
      </Section>

      <Section title="External links">
        <p>
          The Service may contain links to external websites. We are not
          responsible for the content of those sites; following such a link
          subjects you to the linked site's own terms and privacy practices.
        </p>
      </Section>

      <Section title="Changes">
        <p>
          We may update this notice. The effective date at the top reflects the
          latest published version.
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
