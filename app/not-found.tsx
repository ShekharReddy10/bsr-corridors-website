import { hotel, whatsappHref } from "@/content/hotel";

export default function NotFound() {
  const wa = whatsappHref();
  return (
    <main className="not-found">
      <div className="container">
        <p className="eyebrow">Page not found</p>
        <h1>This room doesn’t exist</h1>
        <p className="lead">The page you’re looking for has moved or never existed.</p>
        <div className="btn-row">
          <a className="btn btn-gold" href="/">
            Back to {hotel.name}
          </a>
          {wa && (
            <a className="btn btn-outline-light" href={wa} target="_blank" rel="noopener noreferrer">
              Message us on WhatsApp
            </a>
          )}
        </div>
      </div>
    </main>
  );
}
