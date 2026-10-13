import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { createHash, randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { takeObjectOffline, containerRuntime } from "./runtime-control";
import type { components } from "../../packages/contracts/api";
import {
  createReviewedBusiness,
  request,
  switchIdentity,
} from "./analytics-business";
import { measureReplayReads } from "./replay-performance";
type Replay = components["schemas"]["ReplayView"];
type Scenario = components["schemas"]["ScenarioDetail"];
type Proposal = components["schemas"]["ProposalView"];
async function capture(page: Page) {
  await page
    .getByLabel("Evidence capture reason", { exact: true })
    .fill(
      "Preserve this exact immutable financial decision for independent review",
    );
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/api/v1/replay/capsules" &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", { name: "Capture and verify decision", exact: true })
      .click(),
  ]);
  expect(response.status()).toBe(201);
  const replay: Replay = await response.json();
  await expect(
    page.getByRole("region", { name: "Selected decision replay" }),
  ).toHaveAttribute("data-capsule-id", replay.id);
  expect(replay.attempts[0].status).toBe("MATCH");
  return replay;
}
async function select(page: Page, replay: Replay) {
  await page.locator(`button[data-capsule-id="${replay.id}"]`).click();
  await expect(
    page.getByRole("region", { name: "Selected decision replay" }),
  ).toHaveAttribute("data-capsule-id", replay.id);
}
async function packet(page: Page, replay: Replay, number = 1) {
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", {
        name: `Download redacted evidence ${number}`,
        exact: true,
      })
      .click(),
  ]);
  const path = await download.path();
  expect(path).not.toBeNull();
  return readFile(path!);
}
async function accessible(page: Page) {
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
}
test("financial delta opens exact replay, returned award hierarchy and redacted evidence survive reload and storage outage", async ({
  page,
}, info) => {
  page.setDefaultTimeout(25000);
  const label = `Decision replay ${info.project.name} ${randomUUID().slice(0, 8)}`;
  const business = await createReviewedBusiness(page, label);
  let scenario = await request<Scenario>(
    page,
    `/api/v1/scenarios/${business.scenarioId}`,
  );
  scenario = await request<Scenario>(
    page,
    `/api/v1/scenarios/${scenario.id}/submit`,
    { reason: "Submit the exact independently verified 12-month cost" },
    scenario.version,
  );
  await switchIdentity(page, "finance");
  scenario = await request<Scenario>(
    page,
    `/api/v1/scenarios/${scenario.id}/review`,
    { reason: "Inspect the pinned synthetic financial plan" },
    scenario.version,
  );
  scenario = await request<Scenario>(
    page,
    `/api/v1/scenarios/${scenario.id}/decisions`,
    {
      reason: "Approve the verified synthetic financial calculation",
      decision: "approve",
    },
    scenario.version,
  );
  expect(scenario.state).toBe("APPROVED");
  await page.goto("/scenarios");
  await page
    .getByRole("button", { name: `${label} leap-day plan`, exact: true })
    .click();
  await page
    .getByRole("link", { name: "Replay this 12-month cost delta", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Decision Replay", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Verified decision version", { exact: true }),
  ).toHaveValue(business.revisionId);
  const replay = await capture(page);
  expect(replay.aggregate?.planned.total_minor).toBe("77728752452");
  expect(replay.aggregate?.baseline.total_minor).toBe("77714704176");
  expect(replay.aggregate?.delta_minor).toBe("14048276");
  await expect(page.getByTestId("replay-delta")).toContainText("140,482.76");
  await expect(page.locator('[data-decision-action="APPROVE"]')).toHaveCount(1);
  const original = await packet(page, replay);
  expect(JSON.parse(original.toString()).evidence.expected_sha256).toBe(
    replay.attempts[0].observed_sha256,
  );
  expect(original.toString()).not.toContain("SYN-");
  expect(original.toString()).not.toContain("annual_pay_minor");
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-forecast.png`,
    fullPage: true,
  });
  await accessible(page);
  await measureReplayReads(page, replay, info.project.name);
  await page.getByTestId("replay-delta").scrollIntoViewIfNeeded();
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-forecast-viewport.png`,
  });
  await page.reload();
  await select(page, replay);
  await expect(page.getByTestId("replay-delta")).toContainText("140,482.76");
  const digest = createHash("sha256").update(original).digest("hex");
  const storage = await takeObjectOffline(digest);
  expect(storage.original).toEqual(original);
  try {
    const response = await page.request.get(
      `/api/v1/replay/capsules/${replay.id}/attempts/${replay.attempts[0].id}/export`,
    );
    expect(response.status()).toBe(503);
    await page
      .getByRole("button", { name: "Refresh persisted evidence", exact: true })
      .click();
    await expect(
      page.getByRole("button", {
        name: "Download redacted evidence 1",
        exact: true,
      }),
    ).toBeDisabled();
    await expect(
      page.getByRole("region", { name: "Replay verification attempt table" }),
    ).toContainText("replay_storage_unavailable");
    await page.screenshot({
      path: `docs/evidence/p08-${info.project.name}-storage-outage.png`,
      fullPage: true,
    });
  } finally {
    await storage.restore();
  }
  await page
    .getByRole("button", { name: "Refresh persisted evidence", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Download redacted evidence 1",
      exact: true,
    }),
  ).toBeEnabled();
  expect(await packet(page, replay)).toEqual(original);
  await page
    .getByLabel("Verification reason", { exact: true })
    .fill(
      "Reproduce the original pinned inputs after restoring artifact storage",
    );
  const [verified] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith(`/capsules/${replay.id}/verify`) &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", {
        name: "Run deterministic verification",
        exact: true,
      })
      .click(),
  ]);
  expect(verified.status()).toBe(200);
  const repeated: Replay = await verified.json();
  expect(repeated.sha256).toBe(replay.sha256);
  expect(repeated.attempts.map((row) => row.number)).toEqual([2, 1]);
  expect(repeated.attempts[0].observed_sha256).toBe(
    replay.attempts[0].observed_sha256,
  );
  const me = await request<components["schemas"]["Me"]>(page, "/api/v1/me");
  const concurrent = await Promise.all(
    [0, 1].map(() =>
      page.request.post(`/api/v1/replay/capsules/${replay.id}/verify`, {
        data: {
          reason:
            "Concurrent independent verification of the frozen calculation",
        },
        headers: {
          Origin: "http://127.0.0.1:3017",
          "X-CSRF-Token": me.csrf_token,
          "Idempotency-Key": randomUUID(),
          "If-Match": "2",
        },
      }),
    ),
  );
  const concurrentStatuses = concurrent.map((row) => row.status()).sort();
  expect(concurrentStatuses).toEqual([200, 409]);
  await page
    .getByLabel("Verification reason", { exact: true })
    .fill("Retain my purpose after an actual concurrent attempt conflict");
  const [conflict] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith(`/capsules/${replay.id}/verify`) &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", {
        name: "Run deterministic verification",
        exact: true,
      })
      .click(),
  ]);
  expect(conflict.status()).toBe(409);
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Another verification was recorded" }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Verification reason", { exact: true }),
  ).toHaveValue(
    "Retain my purpose after an actual concurrent attempt conflict",
  );
  await page
    .getByRole("button", { name: "Refresh persisted evidence", exact: true })
    .click();
  await expect(page.locator('[data-attempt-number="3"]')).toContainText(
    "MATCH",
  );
  expect(await packet(page, replay)).toEqual(original);
  await switchIdentity(page, "hr");
  const proposals = await request<components["schemas"]["ProposalPage"]>(
    page,
    `/api/v1/compensation/cycles/${business.cycleId}/proposals`,
  );
  const returned = proposals.items.find(
    (row) => row.employee_id === "SYN-000049",
  );
  expect(returned).toBeDefined();
  await page.goto("/compensation");
  await page.getByLabel("Search cycles", { exact: true }).fill(label);
  await page.locator(".comp-cycle-link").filter({ hasText: label }).click();
  await page
    .getByRole("row")
    .filter({ hasText: "SYN-000049" })
    .getByRole("button", { name: "Review details", exact: true })
    .click();
  const detail = page.getByRole("heading", {
    name: "Proposal decisions and provenance",
  });
  await expect(detail).toBeVisible();
  await page
    .getByRole("link", { name: "Replay this compensation award", exact: true })
    .click();
  await expect(page.getByLabel("Decision type", { exact: true })).toHaveValue(
    "AWARD",
  );
  const awardReplay = await capture(page);
  expect(awardReplay.award?.pool_charge_minor).toBe("200000");
  expect(awardReplay.award?.merit_first_year_minor).toBe("66664");
  expect(awardReplay.revision_history.map((row) => row.number)).toEqual([
    1, 2, 3, 4,
  ]);
  await expect(page.getByTestId("replay-award-charge")).toContainText(
    "2,000.00",
  );
  await page.getByTestId("replay-award-charge").scrollIntoViewIfNeeded();
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-award-viewport.png`,
  });
  await expect(page.locator('[data-decision-action="RETURN"]')).toHaveCount(1);
  expect(awardReplay.approval_steps.map((row) => row.role)).toEqual([
    "DIVISION",
    "HR",
    "FINANCE",
  ]);
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-award.png`,
    fullPage: true,
  });
  await accessible(page);
  await switchIdentity(page, "auditor");
  await page.goto(`/audit?kind=AWARD&subject=${returned!.revision_id}`);
  await select(page, awardReplay);
  await expect(
    page.getByText("Redacted reviewer view", { exact: false }),
  ).toBeVisible();
  await expect(page.getByTestId("replay-award-charge")).toHaveCount(0);
  await expect(page.locator('[data-decision-action="RETURN"]')).toHaveCount(1);
  const redacted = await request<Replay>(
    page,
    `/api/v1/replay/capsules/${awardReplay.id}`,
  );
  expect(redacted.award).toBeNull();
  expect(redacted.award_history).toEqual([]);
  expect(JSON.stringify(redacted)).not.toContain("SYN-");
  expect(redacted.decisions.every((row) => row.reason === null)).toBe(true);
  const awardPacket = await packet(page, awardReplay);
  expect(awardPacket.toString()).not.toContain("pool_charge_minor");
  expect(awardPacket.toString()).not.toContain("Synthetic proposal rationale");
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-auditor.png`,
    fullPage: true,
  });
  await accessible(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const scroll = page.getByRole("region", {
    name: "Replay verification attempt table",
  });
  await scroll.focus();
  await scroll.press("ArrowRight");
  expect(await scroll.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
  await accessible(page);
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-mobile.png`,
    fullPage: true,
  });
  await writeFile(
    `docs/evidence/p08-${info.project.name}-journey.json`,
    JSON.stringify(
      {
        scope: containerRuntime
          ? "actual local Docker Compose OIDC/API/PostgreSQL/browser; hosted execution UNVERIFIED"
          : "actual native OIDC/API/PostgreSQL/browser; hosted execution UNVERIFIED",
        snapshot: business.snapshotId,
        scenario: business.scenarioId,
        revision: business.revisionId,
        capsule: replay.id,
        baseline_minor: replay.aggregate!.baseline.total_minor,
        annual_minor: replay.aggregate!.planned.total_minor,
        delta_minor: replay.aggregate!.delta_minor,
        forecast_sha256: replay.attempts[0].observed_sha256,
        cycle: business.cycleId,
        award_capsule: awardReplay.id,
        returned_revision: returned!.revision_id,
        award_charge_minor: awardReplay.award!.pool_charge_minor,
        first_year_minor: awardReplay.award!.merit_first_year_minor,
        approvals: awardReplay.approval_steps.map((row) => row.role),
        source_and_event_integrity: replay.integrity_issues,
        storage_outage_status: 503,
        restored_original_bytes: true,
        concurrent_verification_statuses: concurrentStatuses,
        redacted: true,
      },
      null,
      2,
    ) + "\n",
  );
});

test("executive and unrelated managers cannot retrieve replay, export or audit; auditor can inspect the actual denial", async ({
  page,
}, info) => {
  await switchIdentity(page, "hr");
  const options = await request<components["schemas"]["ScenarioOptions"]>(
    page,
    "/api/v1/scenarios/options?source_search=June%20workforce",
  );
  const source = options.snapshots.find(
    (row) => row.label === "June workforce · corrected",
  )!;
  const fx = options.fx_sets.find(
    (row) => row.label === "Synthetic USD planning fixture",
  )!;
  const scenario = await request<Scenario>(page, "/api/v1/scenarios", {
    title: `Decision replay restricted ${randomUUID().slice(0, 8)}`,
    base_snapshot_id: source.id,
    policy_id: options.policies[0].id,
    fx_set_id: fx.id,
    reviewer_id: "finance",
    year: 2028,
    planning_date: "2027-12-01",
    scope_orgs: ["D01", "D02"],
    budget_minor: "99999999999999999999",
  });
  await expect
    .poll(
      async () =>
        (await request<Scenario>(page, `/api/v1/scenarios/${scenario.id}`))
          .verified,
      { timeout: 30000 },
    )
    .toBe(true);
  const revision = scenario.revisions[0].id;
  const replay = await request<Replay>(page, "/api/v1/replay/capsules", {
    kind: "SCENARIO",
    subject_id: revision,
    reason: "Capture for actual negative access verification",
  });
  let correlation = "";
  for (const persona of [
    "executive",
    "manager-a",
    "manager-b",
    "steward",
    "approver",
  ]) {
    await switchIdentity(page, persona);
    // Restricted routes stay testable by direct navigation even when the
    // server-grant-aware sidebar omits the link.
    await page.goto("/audit");
    await expect(
      page.getByRole("heading", { name: "Decision evidence is restricted" }),
    ).toBeVisible();
    for (const path of [
      "/api/v1/replay/choices",
      "/api/v1/replay/capsules",
      `/api/v1/replay/capsules/${replay.id}`,
      `/api/v1/replay/capsules/${replay.id}/attempts/${replay.attempts[0].id}/export`,
      "/api/v1/replay/audit",
    ]) {
      const response = await page.request.get(path);
      expect(response.status()).toBe(403);
      expect(await response.text()).not.toContain("SYN-");
      correlation = response.headers()["x-request-id"];
    }
  }
  await switchIdentity(page, "auditor");
  await page.getByRole("link", { name: "Audit", exact: true }).click();
  await page.getByLabel("Correlation ID", { exact: true }).fill(correlation);
  await page
    .getByLabel("Permission result", { exact: true })
    .selectOption("DENY");
  await page
    .getByRole("button", { name: "Apply audit filters", exact: true })
    .click();
  const events = page.getByRole("region", { name: "Audit event table" });
  await expect(events).toContainText("security.replay_denied");
  await expect(events).toContainText(correlation);
  await expect(events).not.toContainText(replay.id);
  await accessible(page);
  await page.screenshot({
    path: `docs/evidence/p08-${info.project.name}-denial-audit.png`,
    fullPage: true,
  });
});
