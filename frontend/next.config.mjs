/** @type {import('next').NextConfig} */
const apiUrl = process.env.API_URL || "http://localhost:8000";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      // Next.js needs inline scripts for hydration data; no third-party script sources are allowed.
      "script-src 'self' 'unsafe-inline'" + (process.env.NODE_ENV === "development" ? " 'unsafe-eval'" : ""),
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "connect-src 'self'",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "object-src 'none'",
    ].join("; "),
  },
];

const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // Server-sent events must not be buffered or compressed by the dev proxy.
  compress: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  // Long streamed answers and large uploads go through this proxy; the defaults (30 s, 10 MB) are far too small.
  experimental: { proxyTimeout: 15 * 60 * 1000, proxyClientMaxBodySize: "2gb" },
  // /api is proxied to the backend when the UI is run on its own (npm run dev). In compose, nginx routes /api itself.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default nextConfig;
