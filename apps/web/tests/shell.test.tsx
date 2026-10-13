import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Providers } from "@/components/providers";
import { Shell, visibleNavigation } from "@/components/shell";
import type { Me } from "@/lib/api";
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function identity(id: string, capabilities: string[]): Me {
  const role = id.startsWith("manager") ? "manager" : id;
  return {
    synthetic_only: true,
    id,
    display_name: id,
    grants: [{ role, scope: id.startsWith("manager") ? "D01" : "*" }],
    capabilities,
    policy_version: "authz-v5",
    expires_at: "2027-01-01T00:00:00Z",
    csrf_token: "shell-test",
  };
}

const roleNavigation = [
  {
    id: "hr",
    capabilities: [
      "people.read",
      "people.pay",
      "ingestion.write",
      "planning.write",
      "comp.manage",
      "reviews.manage",
      "analytics.full",
      "operations.read",
      "replay.full",
    ],
    links: [
      "Overview",
      "Workforce",
      "Scenario Lab",
      "Compensation",
      "Talent",
      "Analytics",
      "Operations",
      "Data Quality",
      "Audit",
      "Settings",
    ],
  },
  {
    id: "finance",
    capabilities: [
      "people.read",
      "people.pay",
      "planning.write",
      "comp.read",
      "comp.finance.signoff",
      "analytics.full",
      "operations.read",
      "replay.full",
    ],
    links: [
      "Overview",
      "Workforce",
      "Scenario Lab",
      "Compensation",
      "Analytics",
      "Operations",
      "Audit",
      "Settings",
    ],
  },
  {
    id: "manager-a",
    capabilities: [
      "people.read",
      "people.pay",
      "comp.propose",
      "reviews.write",
    ],
    links: ["Overview", "Workforce", "Compensation", "Talent", "Settings"],
  },
  {
    id: "approver",
    capabilities: [
      "people.read",
      "people.pay",
      "comp.approve",
      "reviews.approve",
    ],
    links: ["Overview", "Workforce", "Compensation", "Talent", "Settings"],
  },
  {
    id: "executive",
    capabilities: ["analytics.read"],
    links: ["Overview", "Workforce", "Analytics", "Settings"],
  },
  {
    id: "auditor",
    capabilities: [
      "comp.evidence",
      "audit.read",
      "analytics.evidence",
      "operations.read",
      "replay.evidence",
    ],
    links: [
      "Overview",
      "Compensation",
      "Talent",
      "Analytics",
      "Operations",
      "Audit",
      "Settings",
    ],
  },
  {
    id: "steward",
    capabilities: ["people.read", "ingestion.write"],
    links: ["Overview", "Workforce", "Data Quality", "Settings"],
  },
] as const;

it.each(roleNavigation)(
  "shows only authorized navigation for $id",
  (persona) => {
    expect(
      visibleNavigation(identity(persona.id, [...persona.capabilities])).map(
        (item) => item.label,
      ),
    ).toEqual(persona.links);
  },
);

it("omits restricted links from the rendered sidebar", async () => {
  const me = identity("steward", ["people.read", "ingestion.write"]);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(me), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
  render(
    <Providers>
      <Shell>{() => <p>authorized content</p>}</Shell>
    </Providers>,
  );
  expect(
    await screen.findByRole("link", { name: "Data Quality" }),
  ).toBeVisible();
  expect(screen.getByRole("link", { name: "Workforce" })).toBeVisible();
  expect(
    screen.queryByRole("link", { name: "Scenario Lab" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "Compensation" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();
});

it("does not render protected children for an unauthenticated session", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
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
