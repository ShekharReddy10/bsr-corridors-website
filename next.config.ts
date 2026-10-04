import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static HTML export to ./out — works on Netlify and on Heroku (served by `serve`).
  output: "export",
  // next/image optimization needs a server; static export serves images as-is.
  images: { unoptimized: true },
  trailingSlash: true,
};

export default nextConfig;
