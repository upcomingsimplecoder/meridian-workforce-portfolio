import type { NextConfig } from "next";
const hosted =
  process.env.VERCEL === "1" ||
  (process.env.APP_ENV !== undefined &&
    !["local", "test"].includes(process.env.APP_ENV));
const apiOrigin = process.env.API_INTERNAL_ORIGIN || "http://127.0.0.1:8017";
if (hosted) {
  const url = new URL(apiOrigin);
  if (
    url.protocol !== "https:" ||
    url.origin !== apiOrigin ||
    url.username ||
    url.password ||
    ["localhost", "127.0.0.1", "::1"].includes(url.hostname)
  ) {
    throw new Error("Hosted frontend requires the exact HTTPS API origin");
  }
}
const config: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/auth/:path*", destination: `${apiOrigin}/auth/:path*` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          ...(hosted
            ? [{ key: "Strict-Transport-Security", value: "max-age=31536000" }]
            : []),
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
        ],
      },
    ];
  },
};
export default config;
