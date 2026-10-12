import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AnalyticsCreateForm } from "@/components/analytics-create";
import { AnalyticsConsole } from "@/components/analytics-console";
import { AnalyticsRunResult } from "@/components/analytics-result";
import { ApiError, type Me } from "@/lib/api";
import {
  getAnalyticalEvidence,
  getAnalyticalOptions,
  getAnalyticalPublic,
  getAnalyticalRun,
  getAnalyticalRuns,
  type AnalyticalOptions,
  type AnalyticalRun,
} from "@/lib/analytics";
vi.mock("@/lib/analytics", async (original) => ({
  ...(await original<typeof import("@/lib/analytics")>()),
  getAnalyticalEvidence: vi.fn(),
  getAnalyticalOptions: vi.fn(),
  getAnalyticalPublic: vi.fn(),
  getAnalyticalRun: vi.fn(),
  getAnalyticalRuns: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});
const options: AnalyticalOptions = {
  snapshots: [
    { id: "source-a", label: "Sealed A", as_of: "2027-06-30" },
    { id: "source-b", label: "Sealed B", as_of: "2027-02-01" },
  ],
  cycles: [{ id: "cycle-a", label: "Approved A", snapshot_id: "source-a" }],
  scenario_revisions: [
    { id: "revision-a", label: "Plan A · revision 2", snapshot_id: "source-a" },
  ],
};
function fill() {
  fireEvent.change(screen.getByLabelText("Published source"), {
    target: { value: "source-a" },
  });
  fireEvent.click(screen.getByLabelText("Include cycle Approved A"));
  fireEvent.click(
    screen.getByLabelText("Include scenario Plan A · revision 2"),
  );
  fireEvent.change(screen.getByLabelText("Analytical purpose"), {
    target: { value: "Reconcile the immutable reviewed versions" },
  });
}
it("preserves the user's pinned draft when the API rejects a run", async () => {
  const submit = vi
    .fn()
    .mockRejectedValue(new ApiError(409, "The locked export is not ready."));
  render(
    <AnalyticsCreateForm options={options} busy={false} submit={submit} />,
  );
  fill();
  fireEvent.click(
    screen.getByRole("button", { name: "Request reconciled run" }),
  );
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Analytical purpose")).toHaveValue(
    "Reconcile the immutable reviewed versions",
  );
  expect(screen.getByLabelText("Include cycle Approved A")).toBeChecked();
  expect(submit).toHaveBeenCalledWith({
    snapshot_id: "source-a",
    as_of: "2027-06-30",
    cycle_ids: ["cycle-a"],
    scenario_revision_ids: ["revision-a"],
    reason: "Reconcile the immutable reviewed versions",
  });
});
it("clears incompatible cycle and scenario selections when the source changes", async () => {
  const submit = vi.fn().mockResolvedValue({});
  render(
    <AnalyticsCreateForm options={options} busy={false} submit={submit} />,
  );
  fill();
  fireEvent.change(screen.getByLabelText("Published source"), {
    target: { value: "source-b" },
  });
  expect(screen.getByLabelText("Business as-of date")).toHaveValue(
    "2027-02-01",
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Request reconciled run" }),
  );
  await waitFor(() => expect(submit).toHaveBeenCalled());
  expect(submit.mock.calls[0][0].cycle_ids).toEqual([]);
  expect(submit.mock.calls[0][0].scenario_revision_ids).toEqual([]);
});
it("disables draft controls through the server acknowledgement boundary", () => {
  const submit = vi.fn();
  const view = render(
    <AnalyticsCreateForm options={options} busy={false} submit={submit} />,
  );
  fill();
  view.rerender(
    <AnalyticsCreateForm options={options} busy={true} submit={submit} />,
  );
  expect(screen.getByLabelText("Published source")).toBeDisabled();
  expect(screen.getByLabelText("Include cycle Approved A")).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Awaiting server acknowledgement…" }),
  ).toBeDisabled();
});
function display(me: Me) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <AnalyticsConsole me={me} />
    </QueryClientProvider>
  );
}
it("executive UI requests only its fixed governed projection", async () => {
  vi.mocked(getAnalyticalPublic).mockResolvedValue({
    pipeline_status: "STALE",
    data_as_of: "2027-06-30",
    source_watermark: "2027-06-30",
    last_successful_sync: "2026-10-10T12:00:00Z",
    headcount: 5000,
    fte: "4900",
    model_version: "model-v1",
    cloud_validation: "UNVERIFIED",
    suppressed: false,
  });
  render(display({ id: "executive", capabilities: ["analytics.read"] } as Me));
  await screen.findByText("5,000");
  expect(screen.getByRole("status")).toHaveTextContent("STALE");
  expect(getAnalyticalRuns).not.toHaveBeenCalled();
  expect(getAnalyticalOptions).not.toHaveBeenCalled();
  expect(getAnalyticalEvidence).not.toHaveBeenCalled();
  expect(getAnalyticalRun).not.toHaveBeenCalled();
});
it("unrelated manager UI performs no analytical request", () => {
  render(
    display({
      id: "manager-b",
      capabilities: ["people.read", "comp.propose"],
    } as Me),
  );
  expect(
    screen.getByRole("heading", { name: "Analytics access is restricted" }),
  ).toBeInTheDocument();
  expect(getAnalyticalRuns).not.toHaveBeenCalled();
  expect(getAnalyticalPublic).not.toHaveBeenCalled();
});
const completedRun: AnalyticalRun = {
  id: "run-1",
  kind: "SYNC",
  status: "PASS",
  pipeline_status: "CURRENT",
  snapshot_title: "Sealed source",
  data_as_of: "2027-06-30",
  source_watermark: "2027-06-30",
  source_batch_id: "batch-a",
  snapshot_id: "source-a",
  source_captured_at: "2027-06-30T12:00:00Z",
  created_at: "2026-10-10T12:00:00Z",
  last_successful_sync: "2026-10-10T12:01:00Z",
  worker_last_seen: "2026-10-10T12:01:00Z",
  parent_run_id: null,
  dropped_event_id: null,
  attempts: 0,
  job_error: null,
  cloud_validation: "UNVERIFIED",
  fte: null,
  employee_versions: null,
  model_version: "model-v1",
  inputs_sha256: "a".repeat(64),
  model_sha256: "b".repeat(64),
  result_sha256: "c".repeat(64),
  headcount: null,
  historical: false,
  compensation: [],
  scenarios: [],
  comparisons: [],
  artifacts: [],
  delivery_event_ids: [],
};
it("a failed manifest download is shown as a failure without a success acknowledgement", async () => {
  const run = completedRun;
  const download = vi
    .fn()
    .mockRejectedValue(new ApiError(503, "Artifact integrity check failed."));
  render(
    <AnalyticsRunResult
      run={run}
      busy={false}
      command={vi.fn()}
      download={download}
    />,
  );
  await act(async () =>
    fireEvent.click(
      screen.getByRole("button", {
        name: "Download verified analytical manifest",
      }),
    ),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Artifact integrity check failed.",
  );
});

it("the run list follows the acknowledged worker result without presenting pending work as complete", async () => {
  const queued: AnalyticalRun = {
    ...completedRun,
    status: "QUEUED",
    result_sha256: null,
    pipeline_status: "PENDING",
  };
  vi.mocked(getAnalyticalRuns).mockResolvedValue({
    items: [queued],
    next_cursor: null,
  });
  let acknowledge!: (run: AnalyticalRun) => void;
  vi.mocked(getAnalyticalRun).mockReturnValue(
    new Promise((resolve) => {
      acknowledge = resolve;
    }),
  );
  render(display({ id: "hr", capabilities: ["analytics.full"] } as Me));
  const listed = await screen.findByRole("button", {
    name: /Sealed source.*QUEUED/,
  });
  fireEvent.click(listed);
  await waitFor(() => expect(getAnalyticalRun).toHaveBeenCalledWith(queued.id));
  expect(listed).toHaveTextContent("QUEUED");
  expect(screen.queryByText("Reconciled")).not.toBeInTheDocument();
  await act(async () => {
    acknowledge(completedRun);
  });
  await waitFor(() => expect(listed).toHaveTextContent("SYNC · PASS"));
  expect(screen.getByText("Reconciled")).toBeInTheDocument();
});

it("keeps the history region focusable and its persisted rows visible while refresh awaits acknowledgement", async () => {
  const refresh = Promise.withResolvers<{
    items: AnalyticalRun[];
    next_cursor: null;
  }>();
  vi.mocked(getAnalyticalRuns)
    .mockResolvedValueOnce({ items: [completedRun], next_cursor: null })
    .mockReturnValueOnce(refresh.promise);
  render(display({ id: "hr", capabilities: ["analytics.full"] } as Me));
  const row = await screen.findByRole("button", {
    name: /Sealed source.*PASS/,
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Refresh persisted state" }),
  );
  const history = screen.getByRole("region", {
    name: "Analytical run history",
  });
  await waitFor(() => expect(history).toHaveAttribute("aria-busy", "true"));
  expect(row).toBeVisible();
  expect(row).toBeDisabled();
  expect(history).toHaveAttribute("tabindex", "0");
  history.focus();
  expect(history).toHaveFocus();
  await act(async () => {
    refresh.resolve({ items: [completedRun], next_cursor: null });
  });
  await waitFor(() => expect(history).toHaveAttribute("aria-busy", "false"));
  expect(row).toBeEnabled();
});

it("refreshes completed run freshness without a reload or user action, retaining its immutable results", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const offline: AnalyticalRun = { ...completedRun, pipeline_status: "OUTAGE" };
  vi.mocked(getAnalyticalRuns)
    .mockReset()
    .mockResolvedValue({ items: [offline], next_cursor: null })
    .mockResolvedValueOnce({ items: [completedRun], next_cursor: null });
  vi.mocked(getAnalyticalRun)
    .mockReset()
    .mockResolvedValue(offline)
    .mockResolvedValueOnce(completedRun);
  render(display({ id: "finance", capabilities: ["analytics.full"] } as Me));
  fireEvent.click(
    await screen.findByRole("button", { name: /Sealed source.*PASS/ }),
  );
  const selected = await screen.findByRole("region", {
    name: "Selected analytical run",
  });
  await waitFor(() =>
    expect(within(selected).getByRole("status")).toHaveTextContent(
      "PASS · CURRENT",
    ),
  );
  expect(getAnalyticalRuns).toHaveBeenCalledTimes(1);
  expect(getAnalyticalRun).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  await waitFor(() => {
    expect(getAnalyticalRuns).toHaveBeenCalledTimes(2);
    expect(getAnalyticalRun).toHaveBeenCalledTimes(2);
    expect(within(selected).getByRole("status")).toHaveTextContent(
      "PASS · OUTAGE",
    );
  });
  expect(within(selected).getByText(completedRun.result_sha256!)).toBeVisible();
  expect(within(selected).getByText("Reconciled")).toBeVisible();
});
