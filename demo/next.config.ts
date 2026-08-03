import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // A self-contained server bundle, so the demo image carries the traced
  // dependencies instead of the whole node_modules tree.
  output: "standalone",
  // This app lives inside the LegalMemory repository, which has its own
  // lockfiles above it. Without this, tracing walks up and picks a root
  // outside the app.
  turbopack: { root: __dirname },
  outputFileTracingRoot: __dirname,
  // `/mcp/` and `/mcp` are the same endpoint, and neither may redirect. Next
  // answers a trailing slash with a 308, and LegalMemory's connector in
  // LegalWork is declared as `{appliance}/mcp/` — the MCP spec's own examples
  // carry the slash too. A redirect on a POST survives well-behaved clients and
  // quietly breaks the rest, so the catch-all route serves both directly.
  skipTrailingSlashRedirect: true,
};

export default nextConfig;
