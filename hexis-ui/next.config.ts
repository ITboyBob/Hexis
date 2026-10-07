import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  output: "standalone",
  ...(process.env.HEXIS_UI_TEST_TARGET === "worker" && process.env.HEXIS_UI_DIST_DIR
    ? { distDir: process.env.HEXIS_UI_DIST_DIR }
    : {}),
  turbopack: {
    root: path.resolve(__dirname),
  },
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "avatars.charhub.io" },
      { protocol: "https", hostname: "ct-cards.storage.character-tavern.com" },
      { protocol: "https", hostname: "sv.risuai.xyz" },
      { protocol: "https", hostname: "realm.risuai.net" },
    ],
  },
};

export default nextConfig;
