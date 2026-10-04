import type { Metadata, Viewport } from "next";
import { Inter, Playfair_Display } from "next/font/google";
import { hotel, isPlaceholder, siteUrl } from "@/content/hotel";
import "./globals.css";

const sans = Inter({ subsets: ["latin"], variable: "--font-sans", display: "swap" });
const serif = Playfair_Display({ subsets: ["latin"], variable: "--font-serif", display: "swap" });

const description = isPlaceholder(hotel.intro)
  ? `${hotel.name} — hotel rooms, photos, location and contact details.`
  : hotel.intro;
const title = `${hotel.name} | Hotel & Co-Live in Gachibowli, Hyderabad`;
const shareImage = { url: "/og-image.jpg", width: 1200, height: 630, alt: `${hotel.name} — ${hotel.tagline}` };

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title,
  description,
  alternates: { canonical: "/" },
  openGraph: {
    title,
    description,
    type: "website",
    url: "/",
    siteName: hotel.name,
    locale: "en_IN",
    images: [shareImage],
  },
  twitter: { card: "summary_large_image", title, description, images: [shareImage.url] },
};

export const viewport: Viewport = { themeColor: "#1f2a30" };

/**
 * Hotel details in schema.org format, so search engines can show address, phone and map info.
 * City/PIN and check-in/out times are written here in machine format — keep them in sync with content/hotel.ts.
 */
const structuredData = {
  "@context": "https://schema.org",
  "@type": "Hotel",
  name: hotel.name,
  description,
  url: siteUrl,
  image: [`${siteUrl}/og-image.jpg`, `${siteUrl}${hotel.heroPhoto.src}`],
  logo: `${siteUrl}/icon.png`,
  telephone: hotel.contact.phones,
  email: hotel.contact.email || undefined,
  address: {
    "@type": "PostalAddress",
    streetAddress: hotel.location.addressLines.slice(0, 2).join(", "),
    addressLocality: "Hyderabad",
    addressRegion: "Telangana",
    postalCode: "500032",
    addressCountry: "IN",
  },
  geo: {
    "@type": "GeoCoordinates",
    latitude: hotel.location.coordinates.lat,
    longitude: hotel.location.coordinates.lng,
  },
  hasMap: hotel.location.mapsLink || undefined,
  checkinTime: "11:00",
  checkoutTime: "10:00",
  amenityFeature: hotel.amenities
    .filter((a) => !isPlaceholder(a))
    .map((name) => ({ "@type": "LocationFeatureSpecification", name, value: true })),
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${serif.variable}`}>
      <body>
        {children}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(structuredData).replace(/</g, "\\u003c") }}
        />
      </body>
    </html>
  );
}
