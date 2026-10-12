import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Providers } from "@/components/providers";
import { Shell } from "@/components/shell";
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("does not render protected children for an unauthenticated session", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: "Sign in" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        }),
      ),
  );
  render(
    <Providers>
      <Shell>{() => <p>restricted payload</p>}</Shell>
    </Providers>,
  );
  expect(
    await screen.findByRole("link", { name: /Sign in to Meridian/ }),
  ).toHaveAttribute("href", "/auth/login");
  expect(screen.queryByText("restricted payload")).not.toBeInTheDocument();
});

it("shows a recoverable error rather than silently treating API failure as signed out", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new TypeError("Network failed")),
  );
  render(
    <Providers>
      <Shell>{() => <p>restricted payload</p>}</Shell>
    </Providers>,
  );
  expect(
    await screen.findByRole("button", { name: "Try again" }),
  ).toBeVisible();
  expect(
    screen.queryByRole("link", { name: /Sign in to Meridian/ }),
  ).not.toBeInTheDocument();
});
