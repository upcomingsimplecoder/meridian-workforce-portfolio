import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { WorkforceContent } from "@/components/workforce";
import type { Me } from "@/lib/api";
import {
  exportPeople,
  getOrgTree,
  getPeople,
  getSnapshots,
  getSummary,
  type Person,
  type Snapshot,
} from "@/lib/workforce";

vi.mock("@/lib/workforce", async (original) => ({
  ...(await original<typeof import("@/lib/workforce")>()),
  exportPeople: vi.fn(),
  getOrgTree: vi.fn(),
  getPeople: vi.fn(),
  getSnapshots: vi.fn(),
  getSummary: vi.fn(),
}));
vi.mock("@/components/person-detail", () => ({
  PersonDetail: ({ id }: { id: string }) => (
    <section role="dialog">{id}</section>
  ),
}));
const snapshots: Snapshot[] = [
  {
    id: "june",
    title: "June corrected",
    captured_at: "2027-07-01T00:00:00Z",
    source_batch_id: "batch-june",
    source_watermark: "2027-06-30",
    sha256: "a".repeat(64),
  },
  {
    id: "february",
    title: "February freeze",
    captured_at: "2027-03-01T00:00:00Z",
    source_batch_id: "batch-february",
    source_watermark: "2027-02-28",
    sha256: "b".repeat(64),
  },
];
const person: Person = {
  id: "SYN-000037",
  version_id: "version",
  display_name: "Synthetic Context Fixture",
  org_id: "D01",
  org_name: "Synthetic Department",
  manager_id: null,
  position_id: "POS-000037",
  job_family: "Research",
  grade: "L1",
  region: "Americas",
  status: "ACTIVE",
  fte: "1.0000",
  valid_from: "2027-01-01",
  valid_to: null,
  recorded_at: "2027-01-01T00:00:00Z",
  source_batch_id: "batch-june",
};
const me = {
  id: "hr",
  capabilities: ["people.read", "people.export"],
  policy_version: "fixture-v1",
} as Me;
const clients: QueryClient[] = [];
function view(principal = me) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  clients.push(client);
  const component = (identity: Me) => (
    <QueryClientProvider client={client}>
      <WorkforceContent me={identity} />
    </QueryClientProvider>
  );
  const result = render(component(principal));
  return {
    ...result,
    as: (identity: Me) => result.rerender(component(identity)),
  };
}
beforeEach(() => {
  vi.mocked(getSnapshots).mockResolvedValue(snapshots);
  vi.mocked(getSummary).mockImplementation(async (filters) => ({
    ...filters,
    headcount: 5000,
    fte: "4900.0000",
    scope_label: "Synthetic fixture scope",
    snapshot: snapshots.find((row) => row.id === filters.snapshot_id)!,
    departments: [],
    calculated_at: "2027-07-01T00:00:00Z",
    metric_definition_version: "headcount-active-v1",
    suppressed: false,
  }));
  vi.mocked(getOrgTree).mockResolvedValue([
    {
      id: "group",
      name: "Synthetic Group",
      parent_id: null,
      region: "Americas",
    },
    {
      id: "D01",
      name: "Synthetic Department",
      parent_id: "group",
      region: "Americas",
    },
  ]);
  vi.mocked(getPeople).mockImplementation(async (filters) => ({
    as_of: filters.as_of,
    can_view_pay: false,
    items: [person],
    next_cursor: filters.cursor ? null : "context-bound-cursor",
    scope_label: "Synthetic fixture scope",
    snapshot: snapshots.find((row) => row.id === filters.snapshot_id)!,
    total: filters.q ? 1 : 5000,
  }));
  vi.mocked(exportPeople).mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
  vi.resetAllMocks();
});
it.each([
  ["Effective date", "2027-02-28", "june", "2027-02-28"],
  ["Source snapshot", "february", "february", "2027-06-30"],
])(
  "keeps the live search field, draft and focus when %s commits after typing",
  async (label, value, snapshot, date) => {
    view();
    const input = await screen.findByLabelText("Search people");
    input.focus();
    fireEvent.change(input, { target: { value: "SYN-000037" } });
    // A browser may commit a date change on blur after the user has typed.
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    expect(screen.getByLabelText("Search people")).toBe(input);
    expect(input).toHaveValue("SYN-000037");
    expect(input).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(getPeople).toHaveBeenLastCalledWith(
        expect.objectContaining({
          snapshot_id: snapshot,
          as_of: date,
          q: "SYN-000037",
          cursor: undefined,
        }),
      ),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("People explorer")).toHaveTextContent(
        "1 matching people",
      ),
    );
  },
);
it("keeps the applied search but resets department, cursor and person detail before querying a new historical context", async () => {
  view();
  fireEvent.change(await screen.findByLabelText("Search people"), {
    target: { value: "SYN-000037" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() =>
    expect(screen.getByLabelText("People explorer")).toHaveTextContent(
      "1 matching people",
    ),
  );
  fireEvent.change(screen.getByLabelText("Department", { exact: true }), {
    target: { value: "D01" },
  });
  await waitFor(() =>
    expect(getPeople).toHaveBeenLastCalledWith(
      expect.objectContaining({ org: "D01" }),
    ),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Next" }));
  await waitFor(() =>
    expect(getPeople).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: "context-bound-cursor" }),
    ),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "Synthetic Context Fixture SYN-000037",
    }),
  );
  expect(screen.getByRole("dialog")).toBeVisible();
  const callsBefore = vi.mocked(getPeople).mock.calls.length;
  fireEvent.change(screen.getByLabelText("Effective date"), {
    target: { value: "2027-02-28" },
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Department", { exact: true })).toHaveValue("");
  expect(screen.getByLabelText("Search people")).toHaveValue("SYN-000037");
  await waitFor(() =>
    expect(getPeople).toHaveBeenLastCalledWith(
      expect.objectContaining({
        as_of: "2027-02-28",
        q: "SYN-000037",
        org: undefined,
        cursor: undefined,
      }),
    ),
  );
  expect(
    vi
      .mocked(getPeople)
      .mock.calls.slice(callsBefore)
      .every(
        ([filters]) =>
          filters.cursor === undefined && filters.org === undefined,
      ),
  ).toBe(true);
  await screen.findByText("Page 1 · Up to 25 records per page");
});
it.each(["success", "failure"])(
  "ignores a delayed export %s message after leaving its historical context",
  async (outcome) => {
    let finish!: () => void;
    let fail!: (error: Error) => void;
    vi.mocked(exportPeople).mockImplementationOnce(
      () =>
        new Promise<void>((resolve, reject) => {
          finish = resolve;
          fail = reject;
        }),
    );
    view();
    await screen.findByLabelText("Search people");
    fireEvent.click(screen.getByRole("button", { name: "Export matching" }));
    expect(screen.getByText("Preparing export…")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Effective date"), {
      target: { value: "2027-02-28" },
    });
    expect(screen.queryByText("Preparing export…")).not.toBeInTheDocument();
    await act(async () => {
      if (outcome === "success") finish();
      else fail(new Error("Previous context export failure"));
    });
    expect(
      screen.queryByText("Export downloaded for your current permitted scope."),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Previous context export failure"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Export matching" }),
    ).toBeEnabled();
  },
);
it("does not carry search text or a current person detail to another principal", async () => {
  const rendered = view();
  fireEvent.change(await screen.findByLabelText("Search people"), {
    target: { value: "SYN-000037" },
  });
  fireEvent.click(
    screen.getByRole("button", {
      name: "Synthetic Context Fixture SYN-000037",
    }),
  );
  expect(screen.getByRole("dialog")).toBeVisible();
  rendered.as({ ...me, id: "steward", capabilities: ["people.read"] });
  await waitFor(() =>
    expect(screen.getByLabelText("Search people")).toHaveValue(""),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Export matching" }),
  ).not.toBeInTheDocument();
});

it("finds a historical source outside the recent list and reads its pinned date", async () => {
  vi.mocked(getSnapshots).mockImplementation(async (search) =>
    search === "February" ? [snapshots[1]] : [snapshots[0]],
  );
  view();
  await screen.findByLabelText("Search people");
  expect(
    screen.queryByRole("option", { name: "February freeze" }),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Search published sources"), {
    target: { value: "February" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Find source" }),
  );
  await screen.findByRole("option", { name: "February freeze" });
  fireEvent.change(screen.getByLabelText("Source snapshot"), {
    target: { value: "february" },
  });
  await waitFor(() =>
    expect(getPeople).toHaveBeenLastCalledWith(
      expect.objectContaining({ snapshot_id: "february", as_of: "2027-06-30" }),
    ),
  );
  expect(getSnapshots).toHaveBeenLastCalledWith("February");
  expect(screen.getByLabelText("Source snapshot")).toHaveValue("february");
});
it("acknowledges an export after opening a person in the same historical context", async () => {
  const exportRequest = Promise.withResolvers<void>();
  vi.mocked(exportPeople).mockReturnValueOnce(exportRequest.promise);
  view();
  const row = await screen.findByRole("button", {
    name: "Synthetic Context Fixture SYN-000037",
  });
  fireEvent.click(screen.getByRole("button", { name: "Export matching" }));
  fireEvent.click(row);
  expect(screen.getByRole("dialog")).toBeVisible();
  await act(async () => exportRequest.resolve());
  expect(
    screen.getByText("Export downloaded for your current permitted scope."),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Export matching" })).toBeEnabled();
});
