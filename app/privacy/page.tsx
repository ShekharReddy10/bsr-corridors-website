import type { Metadata } from "next";
import { hotel } from "@/content/hotel";

export const metadata: Metadata = {
  title: `Privacy policy | ${hotel.name}`,
  description: `How ${hotel.name} collects, uses and protects guest information.`,
  alternates: { canonical: "/privacy/" },
};

const UPDATED = "7 October 2026";

export default function Privacy() {
  const { contact, location } = hotel;
  return (
    <main className="legal">
      <div className="container legal-inner">
        <a className="legal-back" href="/">
          ← {hotel.name}
        </a>
        <h1>Privacy policy</h1>
        <p className="legal-updated">Last updated: {UPDATED}</p>

        <p>
          This policy explains how {hotel.name} (“we”, “us”), {location.addressLines.join(", ")}, collects and uses
          information about guests and visitors to this website.
        </p>

        <h2>This website</h2>
        <p>
          This website does not use analytics or advertising cookies and does not ask you to create an account. When
          you tap WhatsApp, Call or Email, you contact us directly through that app. The map on the Location section is
          provided by Google Maps, which may set its own cookies under{" "}
          <a href="https://policies.google.com/privacy" target="_blank" rel="noopener noreferrer">
            Google’s privacy policy
          </a>
          .
        </p>

        <h2>Information we collect when you stay with us</h2>
        <p>At check-in we record:</p>
        <ul>
          <li>your name, phone number, address and nationality;</li>
          <li>your identity proof type and number (for example Aadhaar, passport or driving licence);</li>
          <li>for foreign nationals: passport and visa details and date of arrival in India;</li>
          <li>your check-in and check-out dates, room, number of guests and payments.</li>
        </ul>

        <h2>Why we use it</h2>
        <ul>
          <li>to register your stay and manage your booking, room and payments;</li>
          <li>to meet legal requirements for guest registration, including Form C for foreign nationals;</li>
          <li>to contact you about your stay.</li>
        </ul>
        <p>We do not sell your information or use it for advertising.</p>

        <h2>How we store and protect it</h2>
        <p>
          Guest records are kept in a private, password-protected system used only by the hotel’s management. Identity
          numbers are stored encrypted and are shown masked. Backups are kept in the hotel’s own Google Drive account.
          Our service providers (Render for the application, Supabase for the database and Google for backups) store
          data on our behalf.
        </p>

        <h2>Who we share it with</h2>
        <p>
          We share guest information only when required by law — for example with the police or the Foreigners Regional
          Registration Office (FRRO) — or with the service providers above who store it for us.
        </p>

        <h2>How long we keep it</h2>
        <p>
          We keep guest records only as long as needed for the purposes above and for legal and accounting
          requirements, and then delete them.
        </p>

        <h2>Your choices</h2>
        <p>
          You can ask to see, correct or delete the information we hold about you, subject to what we must keep by law.
          Contact us using the details below.
        </p>

        <h2>Contact</h2>
        <p>
          {hotel.name}
          <br />
          {location.addressLines.map((l) => (
            <span key={l}>
              {l}
              <br />
            </span>
          ))}
          {contact.email && (
            <>
              Email: <a href={`mailto:${contact.email}`}>{contact.email}</a>
              <br />
            </>
          )}
          {contact.phones.length > 0 && <>Phone: {contact.phones.join(", ")}</>}
        </p>
      </div>
    </main>
  );
}
