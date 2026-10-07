import type { MetadataRoute } from "next";
import { siteUrl } from "@/content/hotel";

export const dynamic = "force-static";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: `${siteUrl}/`, lastModified: new Date(), changeFrequency: "monthly", priority: 1 },
    { url: `${siteUrl}/privacy/`, lastModified: new Date(), changeFrequency: "yearly", priority: 0.3 },
  ];
}
