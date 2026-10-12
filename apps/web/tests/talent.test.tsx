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
import { TalentPacketEditor } from "@/components/talent-packet";
it("waiting exports begin actual verification when the persisted worker readiness changes", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const cycle = {
    id: fixture().cycle_id,
    lock_number: 1,
    state: "LOCKED",
    current_export_ready: false,
    current_export_failed: false,
  } as TalentCycle;
  const display = (ready: boolean) => (
    <QueryClientProvider client={client}>
      <TalentExportPanel
        me={me}
        cycle={{ ...cycle, current_export_ready: ready }}
      />
    </QueryClientProvider>
  );
  const view = render(display(false));
  expect(
    screen.getByText("Independent worker export is pending."),
  ).toBeInTheDocument();
  expect(getTalentExport).not.toHaveBeenCalled();
  vi.mocked(getTalentExport).mockResolvedValue({
    cycle_id: cycle.id,
    lock_number: 1,
    lock_sha256: "a".repeat(64),
    export_sha256: "b".repeat(64),
    row_count: 2,
    current: true,
    expires_in_seconds: 300,
    download_path: "/unit/download",
  });
  view.rerender(display(true));
  await screen.findByRole("button", { name: "Download review JSON" });
  expect(getTalentExport).toHaveBeenCalledWith(cycle.id, 1);
  expect(
    screen.getByText("Current locked export · 2 packets"),
  ).toBeInTheDocument();
});
import { TalentCreateForm } from "@/components/talent-create";
import { TalentExportPanel } from "@/components/talent-studio";
import { ApiError, type Me } from "@/lib/api";
import {
  getTalentPacket,
  getTalentExport,
  talentPacketCommand,
  type TalentPacket,
  type TalentOptions,
  type TalentCycle,
} from "@/lib/talent";
vi.mock("@/lib/talent", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/talent")>()),
  getTalentPacket: vi.fn(),
  getTalentExport: vi.fn(),
  talentPacketCommand: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const me = { id: "manager-a", csrf_token: "unit-fixture" } as Me;
function fixture(changes: Partial<TalentPacket> = {}): TalentPacket {
  return {
    id: "5e3d556b-75f7-469e-8977-28d6c15956dd",
    cycle_id: "b3a9becb-b257-48e0-885c-5d9b78c3c767",
    employee_id: "SYN-000025",
    display_name: "Synthetic Worker 000025",
    author_id: "manager-a",
    calibrator_id: "approver",
    hr_signer_id: "hr",
    source_version_id: "c0dacd9f-ac45-4577-a47e-7c10ab08c57c",
    context: {
      org_id: "D01",
      job_family: "Technology",
      grade: "G04",
      snapshot_id: "b3a9becb-b257-48e0-885c-5d9b78c3c767",
      source_batch_id: "e1f79d0e-6adc-49b1-a016-bf1cc9907385",
      source_version_id: "c0dacd9f-ac45-4577-a47e-7c10ab08c57c",
      employee_id: "SYN-000025",
      display_name: "Synthetic Worker 000025",
      manager_id: "SYN-000001",
      valid_from: "2027-01-01",
      valid_to: null,
    },
    context_sha256: "a".repeat(64),
    state: "DRAFT",
    version: 2,
    current_revision: 1,
    revision: 1,
    revision_id: "e1f79d0e-6adc-49b1-a016-bf1cc9907385",
    review: {
      accomplishments: "Initial synthetic observation",
      collaboration: "Initial synthetic collaboration",
      development: "Initial synthetic development",
      evidence: [],
    },
    revision_sha256: "b".repeat(64),
    checklist: ["EVIDENCE_REQUIRED"],
    can_edit: true,
    can_calibrate: false,
    allowed_actions: ["comment"],
    revisions: [
      {
        id: "e1f79d0e-6adc-49b1-a016-bf1cc9907385",
        number: 1,
        kind: "INITIAL",
        author_id: "hr",
        reason: "Frozen source context",
        sha256: "b".repeat(64),
        parent_sha256: null,
        created_at: "2028-03-31T00:00:00Z",
      },
    ],
    decisions: [],
    signatures: [],
    decisions_next_version: null,
    calibrations: [],
    comments: [],
    comments_next_cursor: null,
    ...changes,
  };
}
function editor(packet = fixture(), onAcknowledged = vi.fn()) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      <TalentPacketEditor
        me={me}
        initial={packet}
        onAcknowledged={onAcknowledged}
        onClose={vi.fn()}
      />
    </QueryClientProvider>,
  );
}
it("autosave confirms only the acknowledged revision and disables editing while the server is pending", async () => {
  let resolve!: (value: TalentPacket) => void;
  vi.mocked(talentPacketCommand).mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const ack = vi.fn();
  editor(fixture(), ack);
  fireEvent.change(screen.getByLabelText("Accomplishments"), {
    target: { value: "New human-authored synthetic observation" },
  });
  fireEvent.change(screen.getByLabelText("Packet action reason"), {
    target: { value: "Record the observation" },
  });
  await waitFor(() => expect(talentPacketCommand).toHaveBeenCalledTimes(1), {
    timeout: 1800,
  });
  expect(screen.getByText("Saving to server…")).toBeInTheDocument();
  expect(screen.getByLabelText("Accomplishments")).toBeDisabled();
  expect(ack).not.toHaveBeenCalled();
  await act(async () =>
    resolve(
      fixture({
        revision: 2,
        current_revision: 2,
        version: 3,
        review: {
          ...fixture().review,
          accomplishments: "New human-authored synthetic observation",
        },
      }),
    ),
  );
  await screen.findByText("Saved on server · revision 2 · version 3.");
  expect(ack).toHaveBeenCalledTimes(1);
});
it("conflict retains the draft and requires an explicit save against the reloaded current version", async () => {
  vi.mocked(talentPacketCommand).mockRejectedValueOnce(
    new ApiError(409, "Review changed."),
  );
  vi.mocked(getTalentPacket).mockResolvedValue(
    fixture({
      version: 4,
      review: {
        ...fixture().review,
        accomplishments: "Other session saved this text",
      },
    }),
  );
  editor();
  fireEvent.click(
    screen.getByLabelText("Autosave author drafts after a pause"),
  );
  fireEvent.change(screen.getByLabelText("Accomplishments"), {
    target: { value: "Retain this unsaved observation" },
  });
  fireEvent.change(screen.getByLabelText("Packet action reason"), {
    target: { value: "Reasoned correction" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save retained draft" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Accomplishments")).toHaveValue(
    "Retain this unsaved observation",
  );
  expect(
    screen.getByLabelText("Autosave author drafts after a pause"),
  ).not.toBeChecked();
  fireEvent.click(
    screen.getByRole("button", {
      name: "Reload current revision (keep draft)",
    }),
  );
  await waitFor(() => expect(getTalentPacket).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Save retained draft" }),
    ).toBeEnabled(),
  );
  expect(screen.getByLabelText("Accomplishments")).toHaveValue(
    "Retain this unsaved observation",
  );
  vi.mocked(talentPacketCommand).mockResolvedValue(
    fixture({
      version: 5,
      revision: 2,
      current_revision: 2,
      review: {
        ...fixture().review,
        accomplishments: "Retain this unsaved observation",
      },
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Save retained draft" }));
  await screen.findByText("Saved on server · revision 2 · version 5.");
  expect(vi.mocked(talentPacketCommand).mock.calls[1][1].version).toBe(4);
  expect(vi.mocked(talentPacketCommand).mock.calls[1][2]).toEqual({
    action: "review",
    body: {
      reason: "Reasoned correction",
      review: expect.objectContaining({
        accomplishments: "Retain this unsaved observation",
      }),
    },
  });
});
it("calibration changes require an explicit reasoned revision instead of author autosave", async () => {
  editor(
    fixture({
      state: "CALIBRATING",
      can_edit: false,
      can_calibrate: true,
      allowed_actions: ["ack-calibration", "comment"],
    }),
  );
  expect(
    screen.queryByLabelText("Autosave author drafts after a pause"),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Accomplishments"), {
    target: { value: "Clarified observation during human calibration" },
  });
  expect(
    screen.getByRole("button", { name: "Save calibration revision" }),
  ).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Packet action reason"), {
    target: { value: "Clarify observable evidence" },
  });
  vi.mocked(talentPacketCommand).mockResolvedValue(
    fixture({
      state: "CALIBRATING",
      can_edit: false,
      can_calibrate: true,
      revision: 2,
      current_revision: 2,
      review: {
        ...fixture().review,
        accomplishments: "Clarified observation during human calibration",
      },
    }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Save calibration revision" }),
  );
  await screen.findByText("Saved on server · revision 2 · version 2.");
  expect(vi.mocked(talentPacketCommand).mock.calls[0][2].action).toBe(
    "calibration",
  );
});
it("an HR signature requires explicit human acknowledgement and locked narratives have no editor", async () => {
  editor(
    fixture({
      state: "CALIBRATED",
      can_edit: false,
      allowed_actions: ["approve", "return"],
    }),
  );
  fireEvent.change(screen.getByLabelText("Packet action reason"), {
    target: { value: "Independent HR review" },
  });
  expect(
    screen.getByRole("button", { name: "Approve reviewed packet" }),
  ).toBeDisabled();
  fireEvent.click(
    screen.getByLabelText(
      "I reviewed this exact revision and acknowledge my human review responsibility.",
    ),
  );
  vi.mocked(talentPacketCommand).mockResolvedValue(
    fixture({ state: "APPROVED", can_edit: false, allowed_actions: [] }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Approve reviewed packet" }),
  );
  await waitFor(() => expect(talentPacketCommand).toHaveBeenCalledTimes(1));
  expect(vi.mocked(talentPacketCommand).mock.calls[0][2]).toEqual({
    action: "approve",
    body: { reason: "Independent HR review", acknowledgement: true },
  });
  cleanup();
  editor(fixture({ state: "LOCKED", can_edit: false, allowed_actions: [] }));
  expect(
    screen.queryByRole("textbox", { name: "Accomplishments" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Approve reviewed packet" }),
  ).not.toBeInTheDocument();
});
it("duplicate or overlapping participants never leave the cycle configuration form", () => {
  const options: TalentOptions = {
    snapshots: [{ id: "source", label: "Synthetic source" }],
    policies: [{ id: "policy", label: "Human policy" }],
    authors: [{ id: "author", label: "Author" }],
    calibrators: [{ id: "author", label: "Same participant" }],
    hr_signers: [{ id: "hr", label: "HR" }],
  };
  const onCreate = vi.fn();
  render(
    <TalentCreateForm options={options} pending={false} onCreate={onCreate} />,
  );
  fireEvent.change(screen.getByLabelText("Synthetic employee 1"), {
    target: { value: "SYN-000025" },
  });
  fireEvent.change(screen.getByLabelText("Author 1"), {
    target: { value: "author" },
  });
  fireEvent.change(screen.getByLabelText("Calibrator 1"), {
    target: { value: "author" },
  });
  fireEvent.change(screen.getByLabelText("HR signer 1"), {
    target: { value: "hr" },
  });
  fireEvent.submit(
    screen
      .getByRole("button", { name: "Save review configuration" })
      .closest("form")!,
  );
  expect(onCreate).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "three different participants",
  );
});
