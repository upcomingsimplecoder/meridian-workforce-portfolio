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
import { ReplayConsole } from "@/components/replay-console";
import { ReplayDetail } from "@/components/replay-detail";
import { ApiError, type Me } from "@/lib/api";
import {
  captureReplay,
  getReplay,
  getReplayChoices,
  getReplayList,
  verifyReplay,
  type Replay,
} from "@/lib/replay";
vi.mock("next/navigation", () => ({
  useSearchParams: () =>
    new URLSearchParams("kind=SCENARIO&subject=revision-a"),
}));
vi.mock("@/lib/replay", async (original) => ({
  ...(await original<typeof import("@/lib/replay")>()),
  captureReplay: vi.fn(),
  getReplay: vi.fn(),
  getReplayChoices: vi.fn(),
  getReplayList: vi.fn(),
  verifyReplay: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
const replay: Replay = {
  id: "capsule-a",
  kind: "SCENARIO",
  subject_id: "revision-a",
  snapshot_id: "snapshot-a",
  snapshot_sha256: "a".repeat(64),
  source_watermark: "2027-06-30",
  created_at: "2026-10-10T12:00:00Z",
  sha256: "b".repeat(64),
  redacted: false,
  aggregate: null,
  award: null,
  full_operations: [],
  operations: [],
  decisions: [],
  timeline: [],
  source_lineage: [],
  pins: [],
  attempts: [
    {
      id: "attempt-a",
      number: 1,
      status: "MATCH",
      expected_sha256: "c".repeat(64),
      observed_sha256: "c".repeat(64),
      issues: [],
      request_id: "request-a",
      created_at: "2026-10-10T12:00:00Z",
      export_ready: true,
      artifact_issues: [],
    },
  ],
  integrity_issues: [],
  hierarchy: [],
  approval_steps: [],
  revision_history: [],
  award_history: [],
};
function display(
  capabilities = ["replay.full", "replay.verify", "replay.export"],
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const me = { id: "hr", capabilities, grants: [] } as unknown as Me;
  return (
    <QueryClientProvider client={client}>
      <ReplayConsole me={me} />
    </QueryClientProvider>
  );
}
function queries() {
  vi.mocked(getReplayChoices).mockResolvedValue([
    {
      kind: "SCENARIO",
      subject_id: "revision-a",
      label: "Authorized plan · revision 2",
    },
  ]);
  vi.mocked(getReplayList).mockResolvedValue({ items: [], next_cursor: null });
}
it("restricted personas mount no replay or audit data requests", () => {
  render(display(["people.read", "comp.propose"]));
  expect(
    screen.getByRole("heading", { name: "Decision evidence is restricted" }),
  ).toBeInTheDocument();
  expect(getReplayChoices).not.toHaveBeenCalled();
  expect(getReplayList).not.toHaveBeenCalled();
  expect(getReplay).not.toHaveBeenCalled();
});
it("shows a capsule only after the server acknowledges frozen evidence", async () => {
  queries();
  let resolve!: (value: Replay) => void;
  vi.mocked(captureReplay).mockReturnValue(
    new Promise((value) => {
      resolve = value;
    }),
  );
  render(display());
  await screen.findByRole("option", { name: "Authorized plan · revision 2" });
  fireEvent.change(screen.getByLabelText("Evidence capture reason"), {
    target: { value: "Preserve the precise historical cost" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Capture and verify decision" }),
  );
  await waitFor(() => expect(captureReplay).toHaveBeenCalled());
  expect(
    screen.getByRole("button", { name: "Capturing and verifying…" }),
  ).toBeDisabled();
  expect(
    screen.queryByRole("heading", { name: "Reconstruct the decision" }),
  ).not.toBeInTheDocument();
  await act(async () => resolve(replay));
  await screen.findByRole("heading", { name: "Reconstruct the decision" });
  expect(
    screen.getByText("MATCH", { selector: "span.replay-state" }),
  ).toBeInTheDocument();
});
it("retains capture input after a persistence failure", async () => {
  queries();
  vi.mocked(captureReplay).mockRejectedValue(
    new ApiError(503, "Evidence storage is unavailable."),
  );
  render(display());
  await screen.findByRole("option", { name: "Authorized plan · revision 2" });
  fireEvent.change(screen.getByLabelText("Evidence capture reason"), {
    target: { value: "A bounded capture purpose" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Capture and verify decision" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Evidence storage is unavailable.",
  );
  expect(screen.getByLabelText("Evidence capture reason")).toHaveValue(
    "A bounded capture purpose",
  );
  expect(screen.getByLabelText("Verified decision version")).toHaveValue(
    "revision-a",
  );
  expect(
    screen.queryByRole("heading", { name: "Reconstruct the decision" }),
  ).not.toBeInTheDocument();
});
it("a CAS conflict retains the verification reason and acknowledged attempt", async () => {
  queries();
  vi.mocked(getReplayList).mockResolvedValue({
    items: [
      {
        id: replay.id,
        kind: replay.kind,
        subject_id: replay.subject_id,
        sha256: replay.sha256,
        created_at: replay.created_at,
      },
    ],
    next_cursor: null,
  });
  vi.mocked(getReplay).mockResolvedValue(replay);
  vi.mocked(verifyReplay).mockRejectedValue(
    new ApiError(409, "Another verification was recorded. Reload."),
  );
  render(display());
  fireEvent.click(
    await screen.findByRole("button", { name: /SCENARIO · capsule-/ }),
  );
  await screen.findByRole("heading", { name: "Reconstruct the decision" });
  fireEvent.change(screen.getByLabelText("Verification reason"), {
    target: { value: "Verify the original pins" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Run deterministic verification" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Another verification was recorded.",
  );
  expect(screen.getByLabelText("Verification reason")).toHaveValue(
    "Verify the original pins",
  );
  expect(screen.getByRole("row", { name: /1 MATCH/ })).toBeInTheDocument();
});
it("a damaged dependency retains earlier MATCH evidence but disables the download", () => {
  const failed: Replay = {
    ...replay,
    redacted: true,
    integrity_issues: ["PINNED_FX_FINGERPRINT_MISMATCH"],
    attempts: [
      {
        ...replay.attempts[0],
        id: "attempt-b",
        number: 2,
        status: "CORRUPT",
        observed_sha256: null,
        issues: ["PINNED_FX_FINGERPRINT_MISMATCH"],
        export_ready: false,
      },
      { ...replay.attempts[0], export_ready: false },
    ],
  };
  render(
    <ReplayDetail
      replay={failed}
      busy={false}
      verify={vi.fn()}
      download={vi.fn()}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent(
    "PINNED_FX_FINGERPRINT_MISMATCH",
  );
  expect(
    screen.getByRole("button", { name: "Download redacted evidence 1" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Download redacted evidence 2" }),
  ).toBeDisabled();
  expect(screen.getByRole("row", { name: /1 MATCH/ })).toBeInTheDocument();
});
