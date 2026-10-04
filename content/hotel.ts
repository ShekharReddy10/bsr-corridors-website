/**
 * ─────────────────────────────────────────────────────────────
 *  BSR Corridors — website content
 *  Edit this one file to update everything shown on the site.
 * ─────────────────────────────────────────────────────────────
 *
 *  Anything wrapped in [square brackets] is a PLACEHOLDER.
 *  It shows on the site with a dashed "placeholder" marker until
 *  you replace it with real information.
 *
 *  Photos: put your images in /public/images/ and point the `src`
 *  fields below at them, e.g. "/images/lobby.jpg".
 */

export type Photo = {
  src: string;
  alt: string;
  /** Set for portrait (tall) photos so the gallery shows them in a tall tile. */
  tall?: boolean;
};

export type Room = {
  name: string;
  description: string;
  /** Short facts such as bed type, occupancy or size. */
  features: string[];
  photo: Photo;
};

export const hotel = {
  name: "BSR Corridors",
  tagline: "Comfortable stays in the heart of Gachibowli",
  intro:
    "Stay comfortable and feel at home in Gachibowli, Hyderabad. With only four rooms on each floor and a shared kitchen on every floor, BSR Corridors offers a peaceful, spacious and homely stay with fewer guests around — ideal for business trips, families, medical visits, tourists and long stays.",

  contact: {
    /**
     * Hotel numbers, with country code, e.g. "+91 98765 43210".
     * The FIRST number is used for the main "Book on WhatsApp" and "Call" buttons.
     * Each number is listed in the Contact section with WhatsApp and Call links.
     */
    phones: ["+91 70136 84602", "+91 87908 23320"],
    /** Pre-filled message when a guest taps WhatsApp. */
    whatsappMessage: "Hello BSR Corridors, I'd like to enquire about a room booking.",
    /** Optional. Leave "" to hide. */
    email: "bsrcorridors@gmail.com",
  },

  location: {
    addressLines: [
      "1-60/11/5/5, Lakshmi Naga Nilayam",
      "Banjara Nagar, Gachibowli",
      "Hyderabad, Telangana 500032",
    ],
    /** Google Maps share link (Share → Copy link). Leave "" to hide the directions button. */
    mapsLink: "https://maps.app.goo.gl/Ynf988EGZqbhA1pe7",
    /**
     * Google Maps embed URL (Share → Embed a map → copy only the src="…" URL).
     * Leave "" to show a placeholder instead of the map.
     */
    mapsEmbedUrl: "https://www.google.com/maps?q=17.448337,78.364701&z=16&output=embed",
    /** Nearby places guests ask about. Use real distances only. */
    nearby: [
      "Close to Google, TCS, Deloitte & major IT hubs",
      "1 km from AIG Hospital, Gachibowli",
      "Easy access to the Outer Ring Road (ORR)",
      "Convenient access to Rajiv Gandhi International Airport (RGIA)",
    ],
  },

  /** Short trust points shown under the intro. */
  highlights: [
    { title: "Only 4 rooms per floor", text: "A quieter, more spacious stay with fewer guests around." },
    { title: "Shared kitchen on every floor", text: "Cook your own meals and feel at home — great for long stays." },
    { title: "Near IT hubs & AIG Hospital", text: "Close to Google, TCS and Deloitte, and just 1 km from AIG Hospital." },
  ],

  rooms: [
    {
      name: "Luxury AC Room",
      description: "A luxury air-conditioned room with a king-size bed, for up to 2 guests.",
      features: ["King-size bed", "Up to 2 guests", "Air-conditioned"],
      photo: { src: "/images/room-ac.webp", alt: "Luxury AC Room with king-size bed, wall-mounted AC and wardrobe" },
    },
    {
      name: "Luxury Non-AC Room",
      description: "A luxury non-AC room with a king-size bed, for up to 2 guests.",
      features: ["King-size bed", "Up to 2 guests", "Non-AC"],
      photo: { src: "/images/room-non-ac.webp", alt: "Luxury Non-AC Room with king-size bed, ceiling fan, TV and work desk" },
    },
  ] satisfies Room[],

  /** List only facilities the hotel actually offers. */
  amenities: [
    "Shared kitchen on every floor",
    "Free Wi-Fi",
    "Bike parking",
    "Lift",
    "Geyser for hot water",
    "Housekeeping & room cleaning",
    "TV in rooms",
    "Attached bathrooms",
    "CCTV security",
    "Laundry with washing machines",
    "Fridge & microwave in the kitchen",
    "Cooking utensils & cutlery",
    "Water dispensers",
    "Dedicated workspace",
  ],

  policies: [
    { label: "Check-in", value: "11:00 AM – 10:00 PM (later check-in available if you let us know in advance)" },
    { label: "Check-out", value: "10:00 AM" },
    { label: "ID required", value: "Aadhaar or another government photo ID showing your full address, for every guest" },
  ],

  heroPhoto: { src: "/images/hero-lounge.webp", alt: "BSR Corridors lounge with sofa seating and a floral wall mural" } as Photo,

  gallery: [
    { src: "/images/reception.webp", alt: "Reception desk and lobby" },
    { src: "/images/facade.webp", alt: "BSR Corridors building front", tall: true },
    { src: "/images/hero-lounge.webp", alt: "Lounge with sofa seating" },
    { src: "/images/room-wide.webp", alt: "Bedroom with king-size bed, wardrobe and work desk" },
    { src: "/images/room-portrait.webp", alt: "Bedroom with king-size bed and wardrobe", tall: true },
    { src: "/images/room-desk.webp", alt: "Bedroom with work desk and chair" },
    { src: "/images/room-window.webp", alt: "Bedroom with mirror and work desk" },
    { src: "/images/bathroom.webp", alt: "Attached bathroom with geyser", tall: true },
    { src: "/images/kitchen.webp", alt: "Shared kitchen with gas stove, cooking utensils and cutlery" },
    { src: "/images/kitchen-dining.webp", alt: "Shared kitchen with fridge, microwave and dining table" },
    { src: "/images/corridor.webp", alt: "Corridor between rooms", tall: true },
    { src: "/images/lift-lobby.webp", alt: "Lift lobby with seating", tall: true },
    { src: "/images/laundry.webp", alt: "Laundry area with two washing machines" },
  ] as Photo[],
};

/** True for text still wrapped in [brackets]. */
export const isPlaceholder = (text: string) => /^\[.*\]$/s.test(text.trim());

const primaryPhone = () => hotel.contact.phones[0] ?? "";

export const whatsappHref = (message: string = hotel.contact.whatsappMessage, phone: string = primaryPhone()) => {
  const n = phone.replace(/\D/g, "");
  return n ? `https://wa.me/${n}?text=${encodeURIComponent(message)}` : "";
};

export const phoneHref = (phone: string = primaryPhone()) => {
  const n = phone.replace(/[^\d+]/g, "");
  return n ? `tel:${n}` : "";
};
