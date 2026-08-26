import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone build — copies minimal node_modules into .next/standalone
  // so the Docker image can run `node server.js`. ~10x smaller runtime image.
  output: "standalone",
};

export default nextConfig;
