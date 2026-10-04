import Header from "@/components/Header";
import Gallery from "@/components/Gallery";
import Text from "@/components/Text";
import { hotel, phoneHref, whatsappHref } from "@/content/hotel";

function ContactButtons({ light = false }: { light?: boolean }) {
  const wa = whatsappHref();
  const tel = phoneHref();
  if (!wa && !tel) {
    return (
      <p className="placeholder">
        Add a WhatsApp number and phone number in content/hotel.ts to show booking buttons
      </p>
    );
  }
  return (
    <div className="btn-row">
      {wa && (
        <a className="btn btn-whatsapp" href={wa} target="_blank" rel="noopener noreferrer">
          <WhatsAppIcon /> Book on WhatsApp
        </a>
      )}
      {tel && (
        <a className={`btn ${light ? "btn-outline-light" : "btn-outline"}`} href={tel}>
          <PhoneIcon /> Call {hotel.contact.phones[0]}
        </a>
      )}
    </div>
  );
}

export default function Home() {
  const { contact, location } = hotel;
  const wa = whatsappHref();

  return (
    <>
      <Header />

      <main id="top">
        {/* Hero */}
        <section className="hero">
          <img className="hero-bg" src={hotel.heroPhoto.src} alt={hotel.heroPhoto.alt} />
          <div className="hero-overlay" />
          <div className="container hero-content">
            <p className="eyebrow">Welcome to</p>
            <h1>{hotel.name}</h1>
            <p className="hero-tagline">
              <Text value={hotel.tagline} />
            </p>
            <ContactButtons light />
          </div>
        </section>

        {/* About */}
        <section id="about" className="section">
          <div className="container about">
            <div>
              <p className="eyebrow">About the hotel</p>
              <h2>Your stay at {hotel.name}</h2>
              <p className="lead">
                <Text value={hotel.intro} />
              </p>
            </div>
            <ul className="highlights">
              {hotel.highlights.map((h, i) => (
                <li key={i}>
                  <h3>
                    <Text value={h.title} />
                  </h3>
                  <p>
                    <Text value={h.text} />
                  </p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        {/* Rooms */}
        <section id="rooms" className="section section-tint">
          <div className="container">
            <p className="eyebrow">Accommodation</p>
            <h2>Rooms</h2>
            <div className="rooms-grid">
              {hotel.rooms.map((r, i) => (
                <article key={i} className="room-card">
                  <img src={r.photo.src} alt={r.photo.alt} loading="lazy" />
                  <div className="room-body">
                    <h3>
                      <Text value={r.name} />
                    </h3>
                    <p>
                      <Text value={r.description} />
                    </p>
                    <ul className="chips">
                      {r.features.map((f, j) => (
                        <li key={j}>
                          <Text value={f} />
                        </li>
                      ))}
                    </ul>
                    {wa && (
                      <a
                        className="text-link"
                        href={whatsappHref(`Hello ${hotel.name}, I'd like to check availability for: ${r.name}`)}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        Ask about availability →
                      </a>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        {/* Amenities + policies */}
        <section id="amenities" className="section">
          <div className="container amenities">
            <div>
              <p className="eyebrow">Facilities</p>
              <h2>Amenities</h2>
              <ul className="amenity-list">
                {hotel.amenities.map((a, i) => (
                  <li key={i}>
                    <CheckIcon />
                    <Text value={a} />
                  </li>
                ))}
              </ul>
            </div>
            <aside className="policy-card">
              <h3>Good to know</h3>
              <dl>
                {hotel.policies.map((p) => (
                  <div key={p.label}>
                    <dt>{p.label}</dt>
                    <dd>
                      <Text value={p.value} />
                    </dd>
                  </div>
                ))}
              </dl>
            </aside>
          </div>
        </section>

        {/* Gallery */}
        <section id="gallery" className="section section-dark">
          <div className="container">
            <p className="eyebrow">Take a look</p>
            <h2>Gallery</h2>
            <Gallery photos={hotel.gallery} />
          </div>
        </section>

        {/* Location */}
        <section id="location" className="section">
          <div className="container location">
            <div>
              <p className="eyebrow">Find us</p>
              <h2>Location</h2>
              <address>
                <strong>{hotel.name}</strong>
                {location.addressLines.map((line, i) => (
                  <span key={i}>
                    <Text value={line} />
                  </span>
                ))}
              </address>
              <h3 className="nearby-title">Nearby</h3>
              <ul className="nearby">
                {location.nearby.map((n, i) => (
                  <li key={i}>
                    <Text value={n} />
                  </li>
                ))}
              </ul>
              {location.mapsLink && (
                <a className="btn btn-outline" href={location.mapsLink} target="_blank" rel="noopener noreferrer">
                  <PinIcon /> Get directions
                </a>
              )}
            </div>
            <div className="map-frame">
              {location.mapsEmbedUrl ? (
                <iframe
                  src={location.mapsEmbedUrl}
                  title={`Map showing ${hotel.name}`}
                  loading="lazy"
                  referrerPolicy="no-referrer-when-downgrade"
                  allowFullScreen
                />
              ) : (
                <div className="map-placeholder">
                  <PinIcon />
                  <span className="placeholder">Map placeholder — add a Google Maps embed URL in content/hotel.ts</span>
                </div>
              )}
            </div>
          </div>
        </section>

        {/* Contact */}
        <section id="contact" className="section cta">
          <div className="container cta-inner">
            <h2>Ready to stay with us?</h2>
            <p>Message us on WhatsApp or give us a call to check availability and book your room.</p>
            {contact.phones.length > 0 ? (
              <ul className="contact-numbers">
                {contact.phones.map((p) => (
                  <li key={p}>
                    <span className="contact-number">{p}</span>
                    <div className="btn-row">
                      <a className="btn btn-whatsapp" href={whatsappHref(undefined, p)} target="_blank" rel="noopener noreferrer">
                        <WhatsAppIcon /> WhatsApp
                      </a>
                      <a className="btn btn-outline-light" href={phoneHref(p)}>
                        <PhoneIcon /> Call
                      </a>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <ContactButtons light />
            )}
            {contact.email && (
              <p className="cta-email">
                Or email <a href={`mailto:${contact.email}`}>{contact.email}</a>
              </p>
            )}
          </div>
        </section>
      </main>

      <footer className="site-footer">
        <div className="container footer-inner">
          <div>
            <p className="brand">{hotel.name}</p>
            <p>
              {location.addressLines.map((l, i) => (
                <span key={i}>
                  <Text value={l} />
                  {i < location.addressLines.length - 1 ? ", " : ""}
                </span>
              ))}
            </p>
          </div>
          <div className="footer-contact">
            {contact.phones.map((p) => (
              <a key={p} href={phoneHref(p)}>
                {p}
              </a>
            ))}
            {contact.email && <a href={`mailto:${contact.email}`}>{contact.email}</a>}
          </div>
        </div>
        <p className="container copyright">
          © {new Date().getFullYear()} {hotel.name}. All rights reserved.
        </p>
      </footer>

      {wa && (
        <a className="wa-float" href={wa} target="_blank" rel="noopener noreferrer" aria-label="Chat on WhatsApp">
          <WhatsAppIcon />
        </a>
      )}
    </>
  );
}

/* ── Inline icons (no icon library needed) ── */
function WhatsAppIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="currentColor">
      <path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2.05 22l5.25-1.38a9.9 9.9 0 0 0 4.74 1.21c5.46 0 9.91-4.45 9.91-9.91S17.5 2 12.04 2zm0 18.15c-1.48 0-2.93-.4-4.2-1.15l-.3-.18-3.12.82.83-3.04-.2-.31a8.2 8.2 0 0 1-1.26-4.38c0-4.54 3.7-8.24 8.25-8.24 4.54 0 8.24 3.7 8.24 8.24 0 4.55-3.7 8.24-8.24 8.24zm4.52-6.16c-.25-.12-1.47-.72-1.7-.81-.23-.08-.39-.12-.56.13-.16.25-.64.81-.79.97-.14.17-.29.19-.54.06-.25-.12-1.05-.39-1.99-1.23-.74-.66-1.23-1.47-1.38-1.72-.14-.25-.01-.38.11-.51.11-.11.25-.29.37-.43.13-.15.17-.25.25-.42.08-.16.04-.31-.02-.43-.06-.13-.56-1.34-.76-1.84-.2-.48-.41-.42-.56-.43h-.48c-.17 0-.43.06-.66.31-.22.25-.86.85-.86 2.07 0 1.22.89 2.4 1.01 2.56.12.17 1.75 2.67 4.23 3.74.59.26 1.05.41 1.41.52.59.19 1.13.16 1.56.1.48-.07 1.47-.6 1.67-1.18.21-.58.21-1.07.14-1.18-.06-.1-.22-.16-.47-.28z" />
    </svg>
  );
}
function PhoneIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z" />
    </svg>
  );
}
function PinIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
      <circle cx="12" cy="10" r="3" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.5">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
