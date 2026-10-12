import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ScenarioLab } from "@/components/scenario-lab";
import type { Me } from "@/lib/api";
import {
  getScenarioOptions,
  getScenarios,
  runCommand,
  type Command,
  type Scenario,
} from "@/lib/scenarios";

vi.mock("@/lib/scenarios", async (original) => ({
  ...(await original<typeof import("@/lib/scenarios")>()),
  getScenarioOptions: vi.fn(),
  getScenarios: vi.fn(),
  runCommand: vi.fn(),
}));
vi.mock("@/components/scenario-detail", () => ({
  ScenarioDetail: ({
    id,
    onCommand,
  }: {
    id: string;
    onCommand: (row: Scenario, command: Command) => void;
  }) => (
    <section aria-label="Selected plan">
      <h2>{id}</h2>
      <button
        onClick={() =>
          onCommand(plan(id), {
            action: "branches",
            body: {
              title: "Option B",
              reviewer_id: "hr",
              reason: "Independent branch",
            },
          })
        }
      >
        Create test branch
      </button>
      <button
        onClick={() =>
          onCommand(plan(id), {
            action: "submit",
            body: { reason: "Test selected identity" },
          })
        }
      >
        Submit selected plan
      </button>
    </section>
  ),
}));
function plan(id: string): Scenario {
  return {
    id,
    title: id,
    year: 2028,
    state: "DRAFT",
    version: 1,
    revision: 1,
    annual_total_minor: "77714704176",
    currency: "USD",
    exponent: 2,
    budget_minor: "99999999999",
    base_snapshot_id: "source",
    policy_id: "policy",
    fx_set_id: "fx",
    planning_date: "2027-01-01",
    created_at: "2026-10-10T00:00:00Z",
    owner_id: "finance",
    reviewer_id: "hr",
    scope_orgs: ["D01"],
    parent_revision_id: null,
    parent_scenario_id: null,
    pending_job: false,
    failed_job: false,
    manifest_ready: false,
    forecast_sha256: "a".repeat(64),
    verified: true,
    decisions: [],
    revisions: [],
    rules: {},
    operations: [],
  };
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it("cannot select a moving branch row until its acknowledged mutation and list refresh finish, then targets the selected identity", async () => {
  const initial = { items: [plan("Option A")], next_cursor: null };
  let acknowledge!: (value: Scenario) => void;
  let refresh!: (value: typeof initial) => void;
  vi.mocked(getScenarios)
    .mockResolvedValueOnce(initial)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          refresh = resolve;
        }),
    );
  vi.mocked(getScenarioOptions).mockResolvedValue({
    snapshots: [],
    policies: [],
    fx_sets: [],
    scope_orgs: [],
    reviewers: [],
  });
  vi.mocked(runCommand).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        acknowledge = resolve;
      }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ScenarioLab
        me={{ id: "finance", capabilities: ["planning.write"] } as Me}
      />
    </QueryClientProvider>,
  );
  const a = await screen.findByRole("button", {
    name: "Option A",
  });
  fireEvent.click(a);
  fireEvent.click(screen.getByRole("button", { name: "Create test branch" }));
  await waitFor(() => expect(a).toBeDisabled());
  await act(async () => acknowledge(plan("Option B")));
  await waitFor(() => expect(getScenarios).toHaveBeenCalledTimes(2));
  expect(a).toBeDisabled();
  fireEvent.click(a);
  expect(screen.getByRole("heading", { name: "Option B" })).toBeInTheDocument();
  await act(async () =>
    refresh({ items: [plan("Option B"), plan("Option A")], next_cursor: null }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Option A" }),
    ).toBeEnabled(),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Option A" }),
  );
  expect(screen.getByRole("heading", { name: "Option A" })).toBeInTheDocument();
  vi.mocked(runCommand).mockResolvedValueOnce(plan("Option A"));
  vi.mocked(getScenarios).mockResolvedValue({
    items: [plan("Option B"), plan("Option A")],
    next_cursor: null,
  });
  fireEvent.click(screen.getByRole("button", { name: "Submit selected plan" }));
  await waitFor(() => expect(runCommand).toHaveBeenCalledTimes(2));
  expect(vi.mocked(runCommand).mock.calls[1][1].id).toBe("Option A");
});
