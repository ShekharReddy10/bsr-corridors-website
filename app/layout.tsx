import type { Metadata, Viewport } from "next";
import { Inter, Playfair_Display } from "next/font/google";
import { hotel, isPlaceholder } from "@/content/hotel";
import "./globals.css";

const sans = Inter({ subsets: ["latin"], variable: "--font-sans", display: "swap" });
const serif = Playfair_Display({ subsets: ["latin"], variable: "--font-serif", display: "swap" });

const description = isPlaceholder(hotel.intro)
  ? `${hotel.name} — hotel rooms, photos, location and contact details.`
  : hotel.intro;

export const metadata: Metadata = {
  title: `${hotel.name} | Hotel`,
  description,
  openGraph: { title: hotel.name, description, type: "website" },
};

export const viewport: Viewport = { themeColor: "#1f2a30" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${serif.variable}`}>
      <body>{children}</body>
    </html>
  );
}
