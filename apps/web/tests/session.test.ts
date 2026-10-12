import { describe, expect, it } from "vitest";
import { logoutSucceeded } from "@/lib/session";

describe("logout outcome", () => {
  it("clears a stale page when the server already expired the session", () => {
    expect(logoutSucceeded({ ok: false, status: 401 })).toBe(true);
  });

  it("keeps real logout failures visible", () => {
    expect(logoutSucceeded({ ok: false, status: 403 })).toBe(false);
    expect(logoutSucceeded({ ok: false, status: 500 })).toBe(false);
  });
});
