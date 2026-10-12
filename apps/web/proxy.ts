import { NextRequest, NextResponse } from "next/server";

// A new nonce per rendered document. User supplied CSP/nonce headers are replaced.
export async function proxy(request: NextRequest) {
  const allowed = new Set([
    new URL(process.env.WEB_ORIGIN ?? "http://127.0.0.1:3017").hostname,
  ]);
  if (!process.env.APP_ENV || ["local", "test"].includes(process.env.APP_ENV)) {
    allowed.add("127.0.0.1");
    allowed.add("localhost");
  }
  let validHost: boolean;
  try {
    const authority = new URL("http://" + request.headers.get("host"));
    validHost =
      allowed.has(authority.hostname) &&
      !authority.username &&
      !authority.password &&
      authority.pathname === "/" &&
      !authority.search &&
      !authority.hash;
  } catch {
    validHost = false;
  }
  if (!validHost) {
    const requestId = crypto.randomUUID();
    return NextResponse.json(
      {
        type: "about:blank",
        title: "Bad Request",
        status: 400,
        detail: "The request host is not allowed.",
        request_id: requestId,
      },
      {
        status: 400,
        headers: {
          "Content-Type": "application/problem+json",
          "Cache-Control": "no-store",
          "X-Request-ID": requestId,
        },
      },
    );
  }
  // Next clones this stream for rewrites. Bound the clone before forwarding so
  // an API early rejection cannot become an EPIPE/500 at the public boundary.
  if (request.body) {
    const reader = request.body.getReader();
    let bytes = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const expired = new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error("request_body_timeout")),
          15000,
        );
      });
      while (true) {
        const chunk = await Promise.race([reader.read(), expired]);
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > 8 * 1024 * 1024) {
          const requestId = crypto.randomUUID();
          return NextResponse.json(
            {
              type: "about:blank",
              title: "Payload Too Large",
              status: 413,
              detail: "Request exceeds 8 MiB.",
              request_id: requestId,
            },
            {
              status: 413,
              headers: {
                "Content-Type": "application/problem+json",
                "Cache-Control": "no-store",
                "X-Request-ID": requestId,
              },
            },
          );
        }
      }
    } catch {
      const requestId = crypto.randomUUID();
      return NextResponse.json(
        {
          type: "about:blank",
          title: "Request Timeout",
          status: 408,
          detail: "Request body could not be read within the work limit.",
          request_id: requestId,
        },
        {
          status: 408,
          headers: {
            "Content-Type": "application/problem+json",
            "Cache-Control": "no-store",
            "X-Request-ID": requestId,
          },
        },
      );
    } finally {
      clearTimeout(timer);
      reader.releaseLock();
    }
  }
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const development = process.env.NODE_ENV === "development";
  const csp = [
    "default-src 'self'",
    "script-src 'self' 'nonce-" +
      nonce +
      "' 'strict-dynamic'" +
      (development ? " 'unsafe-eval'" : ""),
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
  const headers = new Headers(request.headers);
  if (
    request.nextUrl.pathname.startsWith("/api/") ||
    request.nextUrl.pathname.startsWith("/auth/")
  ) {
    // The rewrite proxy pools upstream sockets without an idle expiry. Ask
    // the API to close each response so a later request cannot reuse a socket
    // that Uvicorn has already closed. Mutations are never retried here.
    headers.set("connection", "close");
  }
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("Cache-Control", "private, no-store");
  if (process.env.COOKIE_SECURE === "true")
    response.headers.set("Strict-Transport-Security", "max-age=31536000");
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
