import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OperationsConsole } from "@/components/operations-console";
import { ApiError, type Me } from "@/lib/api";
import { getOperations, getTrace, type Operations } from "@/lib/operations";
vi.mock("@/lib/operations", () => ({
  getOperations: vi.fn(),
  getTrace: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
const me: Me = {
  synthetic_only: true,
  id: "hr",
  display_name: "Synthetic HR",
  grants: [{ role: "hr", scope: "*" }],
  capabilities: ["operations.read"],
  policy_version: "authz-v5",
  expires_at: "2026-10-11T00:00:00Z",
  csrf_token: "synthetic-unit",
};
function show(identity = me) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <OperationsConsole me={identity} />
    </QueryClientProvider>,
  );
}
it("restricted identities fetch no technical observations or traces", () => {
  show({ ...me, id: "executive", capabilities: ["analytics.read"] });
  expect(
    screen.getByRole("heading", { name: "Operational access is restricted" }),
  ).toBeVisible();
  expect(getOperations).not.toHaveBeenCalled();
  expect(getTrace).not.toHaveBeenCalled();
});
it("a failed observation query displays the server error and offers refresh", async () => {
  vi.mocked(getOperations).mockRejectedValue(
    new ApiError(503, "Telemetry is unavailable."),
  );
  show();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Telemetry is unavailable.",
  );
  expect(
    screen.getByRole("button", { name: "Refresh observations" }),
  ).toBeEnabled();
  expect(
    screen.queryByText("No configured alert threshold is currently breached."),
  ).not.toBeInTheDocument();
});
it("missing telemetry remains unobserved and bounded windows are visibly qualified", async () => {
  const data: Operations = {
    measured_at: "2026-10-10T00:00:00Z",
    window_started_at: "2026-10-09T23:45:00Z",
    sample_limit: 5000,
    sample_truncated: true,
    http_requests: 0,
    http_errors: 0,
    denied_requests: 0,
    http_p50_ms: null,
    http_p95_ms: null,
    pending_jobs: 0,
    dead_jobs: 0,
    oldest_pending_seconds: null,
    workers: [
      {
        name: "outbox",
        last_seen_at: null,
        age_seconds: null,
        state: "UNOBSERVED",
      },
    ],
    last_analytics_publication: null,
    analytics_matches_latest_snapshot: false,
    alerts: [
      { code: "worker_outbox_unobserved", detail: "Worker has no heartbeat." },
    ],
    recent_spans: [],
  };
  vi.mocked(getOperations).mockResolvedValue(data);
  show();
  expect(
    await screen.findByText(/Latest 5000 spans; window is truncated/),
  ).toBeVisible();
  expect(screen.getByText(/outbox worker · UNOBSERVED/)).toBeVisible();
  expect(
    screen.getByText("No completed spans have been recorded in this window."),
  ).toBeVisible();
  expect(screen.getByText(/Worker has no heartbeat/)).toBeVisible();
});
