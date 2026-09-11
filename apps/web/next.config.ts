import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Cloud Run runs the container, not a Node server we manage. `standalone` emits
  // a self-contained server with only the dependencies the app actually imports,
  // which is what keeps the image small enough to cold-start acceptably at
  // min-instances 0.
  output: "standalone",
  // The monorepo root, so the standalone trace picks up files above apps/web.
  outputFileTracingRoot: new URL("../../", import.meta.url).pathname,
  reactStrictMode: true,
  poweredByHeader: false,
  typedRoutes: true,
};

export default nextConfig;
