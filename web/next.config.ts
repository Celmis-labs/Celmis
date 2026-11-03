import type { NextConfig } from "next";

import { STATIC_SECURITY_HEADERS } from "./lib/csp";

const nextConfig: NextConfig = {
  // Standalone build — copies minimal node_modules into .next/standalone
  // so the Docker image can run `node server.js`. ~10x smaller runtime image.
  output: "standalone",
  // `X-Powered-By: Next.js` tells a scanner which framework to try exploits
  // for and tells us nothing.
  poweredByHeader: false,
  // The headers that do not depend on the request. The Content-Security-Policy
  // and Strict-Transport-Security are set per request in proxy.ts (a nonce
  // must be fresh for every response, and HSTS only belongs on https); this
  // covers what the proxy does not run on: /api/auth/*, static files, images.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: STATIC_SECURITY_HEADERS.map(([key, value]) => ({ key, value })),
      },
    ];
  },
};

export default nextConfig;
