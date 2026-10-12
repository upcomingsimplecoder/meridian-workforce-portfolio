// @vitest-environment node
import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "../proxy";

describe("document script policy", () => {
  it("replaces forged policies and makes fresh document nonces with no private caching", async () => {
    const forged = new NextRequest("http://127.0.0.1:3017/audit", {
      headers: {
        host: "127.0.0.1:3017",
        "x-nonce": "forged",
        "Content-Security-Policy": "script-src 'unsafe-inline'",
      },
    });
    const first = await proxy(forged);
    const second = await proxy(forged);
    const csp = first.headers.get("Content-Security-Policy")!;
    expect(csp).toContain("'strict-dynamic'");
    expect(
      csp
        .split(";")
        .map((s) => s.trim())
        .find((s) => s.startsWith("script-src")),
    ).not.toContain("'unsafe-inline'");
    expect(csp).not.toContain("forged");
    expect(csp).not.toEqual(second.headers.get("Content-Security-Policy"));
    expect(first.headers.get("Cache-Control")).toBe("private, no-store");
    expect(
      first.headers.get("x-middleware-request-content-security-policy"),
    ).toBe(csp);
    expect(csp).toContain("object-src 'none'");
  });

  it("rejects a forged host before Next normalizes proxy headers", async () => {
    const response = await proxy(
      new NextRequest("http://127.0.0.1:3017/api/v1/me", {
        headers: { host: "attacker.example" },
      }),
    );
    expect(response.status).toBe(400);
    expect(response.headers.get("x-middleware-next")).toBeNull();
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });

  it("bounds actual body bytes before forwarding while valid bodies can proceed", async () => {
    const init = { method: "POST", headers: { host: "127.0.0.1:3017" } };
    const oversized = await proxy(
      new NextRequest("http://127.0.0.1:3017/api/v1/logout", {
        ...init,
        body: Buffer.alloc(8 * 1024 * 1024 + 1, "x"),
      }),
    );
    expect(oversized.status).toBe(413);
    expect(oversized.headers.get("x-middleware-next")).toBeNull();
    const problem = await oversized.json();
    expect(problem.request_id).toBe(oversized.headers.get("x-request-id"));
    const valid = await proxy(
      new NextRequest("http://127.0.0.1:3017/api/v1/logout", {
        ...init,
        body: "{}",
      }),
    );
    expect(valid.headers.get("x-middleware-next")).toBe("1");
  });
});
