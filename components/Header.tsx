"use client";

import { useEffect, useState } from "react";
import { hotel, whatsappHref } from "@/content/hotel";

const links = [
  { href: "#about", label: "About" },
  { href: "#rooms", label: "Rooms" },
  { href: "#amenities", label: "Amenities" },
  { href: "#gallery", label: "Gallery" },
  { href: "#location", label: "Location" },
  { href: "#contact", label: "Contact" },
];

export default function Header() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const wa = whatsappHref();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header className={`site-header${scrolled || open ? " is-solid" : ""}`}>
      <div className="container header-inner">
        <a href="#top" className="brand" onClick={() => setOpen(false)}>
          {hotel.name}
        </a>
        <button
          className="menu-toggle"
          aria-expanded={open}
          aria-controls="site-nav"
          aria-label={open ? "Close menu" : "Open menu"}
          onClick={() => setOpen((o) => !o)}
        >
          <span />
          <span />
          <span />
        </button>
        <nav id="site-nav" className={`site-nav${open ? " is-open" : ""}`} aria-label="Main">
          {links.map((l) => (
            <a key={l.href} href={l.href} onClick={() => setOpen(false)}>
              {l.label}
            </a>
          ))}
          {wa && (
            <a className="btn btn-gold btn-sm" href={wa} target="_blank" rel="noopener noreferrer">
              Book on WhatsApp
            </a>
          )}
        </nav>
      </div>
    </header>
  );
}
