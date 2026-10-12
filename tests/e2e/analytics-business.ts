import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type { components } from "../../packages/contracts/api";
import { signIn } from "./helpers";
type Cycle = components["schemas"]["CycleView"];
type Proposal = components["schemas"]["ProposalView"];
type Scenario = components["schemas"]["ScenarioDetail"];
type Options = components["schemas"]["CompOptions"];
type PlanningOptions = components["schemas"]["ScenarioOptions"];

export async function switchIdentity(page: Page, persona: string) {
  if (
    await page.getByRole("button", { name: "Sign out", exact: true }).count()
  ) {
    const [response] = await Promise.all([
      page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === "/api/v1/logout" &&
          r.request().method() === "POST",
      ),
      page.getByRole("button", { name: "Sign out", exact: true }).click(),
    ]);
    expect(response.status()).toBe(204);
    await expect(
      page.getByRole("link", { name: /Sign in to Meridian/ }),
    ).toBeVisible();
  }
  await signIn(page, persona);
  test.setTimeout(360000);
}

export async function request<T>(
  page: Page,
  path: string,
  body?: unknown,
  version?: number,
): Promise<T> {
  const identity = await page.request.get("/api/v1/me");
  expect(identity.status()).toBe(200);
  const me: components["schemas"]["Me"] = await identity.json();
  const response =
    body === undefined
      ? await page.request.get(path)
      : await page.request.post(path, {
          data: body,
          headers: {
            Origin: "http://127.0.0.1:3017",
            "X-CSRF-Token": me.csrf_token,
            "Idempotency-Key": randomUUID(),
            ...(version !== undefined ? { "If-Match": String(version) } : {}),
          },
        });
  expect(response.status(), `Acknowledged ${path}`).toBeGreaterThanOrEqual(200);
  expect(response.status(), `Acknowledged ${path}`).toBeLessThan(300);
  return response.json();
}

async function proposal(page: Page, id: string) {
  return request<Proposal>(page, `/api/v1/compensation/proposals/${id}`);
}
async function cycle(page: Page, id: string) {
  return request<Cycle>(page, `/api/v1/compensation/cycles/${id}`);
}
async function proposalAction(
  page: Page,
  id: string,
  action: string,
  body: unknown,
) {
  const current = await proposal(page, id);
  return request<Proposal>(
    page,
    `/api/v1/compensation/proposals/${id}/${action}`,
    body,
    current.version,
  );
}
async function reviewDecision(
  page: Page,
  id: string,
  action: "approve" | "return",
  exceptions: string[] = [],
) {
  const current = await proposal(page, id);
  return request<Proposal>(
    page,
    `/api/v1/compensation/proposals/${id}/decisions`,
    {
      revision_id: current.revision_id,
      decision: action,
      reason: "Independent synthetic review of the exact pinned revision",
      acknowledged_exceptions: exceptions,
    },
    current.version,
  );
}
async function submitted(page: Page, id: string) {
  await expect
    .poll(async () => (await proposal(page, id)).verified, { timeout: 30000 })
    .toBe(true);
  return proposalAction(page, id, "submit", {
    reason: "Submit independently verified synthetic award",
  });
}

export async function createReviewedBusiness(page: Page, label: string) {
  await switchIdentity(page, "hr");
  const options = await request<Options>(
    page,
    "/api/v1/compensation/options?source_search=June%20workforce",
  );
  const snapshot = options.snapshots.find(
    (row) => row.label === "June workforce · corrected",
  );
  expect(snapshot).toBeDefined();
  const guideline = options.guidelines.find(
    (row) => row.label === "Synthetic merit and bonus guideline v1",
  );
  expect(guideline).toBeDefined();
  const fx = options.fx_sets.find(
    (row) => row.label === "Synthetic USD planning fixture",
  );
  expect(fx).toBeDefined();
  let current = await request<Cycle>(page, "/api/v1/compensation/cycles", {
    title: label,
    snapshot_id: snapshot!.id,
    guideline_id: guideline!.id,
    fx_set_id: fx!.id,
    selected_ids: ["SYN-000025", "SYN-000049", "SYN-000073"],
    terms: {
      eligibility_date: "2028-03-31",
      merit_effective_date: "2028-05-01",
      bonus_service_start: "2028-01-01",
      bonus_service_end: "2029-01-01",
      bonus_payment_date: "2029-01-15",
    },
    pools: [
      {
        manager_id: "manager-a",
        org_id: "D01",
        currency: "USD",
        budget_minor: "2000000",
        holdback_minor: "0",
      },
      {
        manager_id: "manager-b",
        org_id: "D01",
        currency: "USD",
        budget_minor: "500000",
        holdback_minor: "100000",
      },
    ],
    approvals: [
      {
        org_id: "D01",
        division_approver_id: "approver",
        hr_signoff_id: "hr",
        finance_signoff_id: "finance",
      },
    ],
    reason: "Freeze three synthetic awards for governed SQL reconciliation",
  });
  current = await request<Cycle>(
    page,
    `/api/v1/compensation/cycles/${current.id}/launch`,
    { reason: "Launch the exact frozen source and reviewer chain" },
    current.version,
  );
  const proposals: Record<string, Proposal> = {};
  for (const manager of ["manager-a", "manager-b"]) {
    await switchIdentity(page, manager);
    const roster = await request<components["schemas"]["ProposalPage"]>(
      page,
      `/api/v1/compensation/cycles/${current.id}/proposals`,
    );
    expect(roster.items.length).toBe(manager === "manager-a" ? 1 : 2);
    for (const row of roster.items) {
      proposals[row.employee_id] = await proposalAction(page, row.id, "award", {
        merit_minor: manager === "manager-a" ? "1000000" : "120000",
        bonus_minor: "100000",
        comment: "Documented synthetic award",
        reason: "Save the reviewed business fixture",
      });
      await submitted(page, row.id);
    }
  }
  await switchIdentity(page, "approver");
  for (const [employee, row] of Object.entries(proposals)) {
    const latest = await proposal(page, row.id);
    await proposalAction(page, row.id, "review", {
      revision_id: latest.revision_id,
      reason: "Review exact submitted synthetic revision",
    });
    await reviewDecision(
      page,
      row.id,
      "approve",
      employee === "SYN-000025" ? ["MERIT_GUIDELINE_EXCEPTION"] : [],
    );
  }
  await switchIdentity(page, "hr");
  const returned = await reviewDecision(
    page,
    proposals["SYN-000049"].id,
    "return",
  );
  expect(returned.revision).toBe(3);
  expect(returned.steps.every((step) => !step.approved)).toBe(true);
  await switchIdentity(page, "manager-b");
  const corrected = await proposalAction(page, returned.id, "award", {
    merit_minor: "100000",
    bonus_minor: "100000",
    comment: "Corrected synthetic rationale",
    reason: "Revise the returned proposal",
  });
  expect(corrected.revision).toBe(4);
  await submitted(page, corrected.id);
  await switchIdentity(page, "approver");
  const revised = await proposal(page, corrected.id);
  await proposalAction(page, revised.id, "review", {
    revision_id: revised.revision_id,
    reason: "Review returned and revised award",
  });
  await reviewDecision(page, revised.id, "approve");
  await switchIdentity(page, "hr");
  for (const row of Object.values(proposals))
    await reviewDecision(page, row.id, "approve");
  await switchIdentity(page, "finance");
  for (const [employee, row] of Object.entries(proposals))
    await reviewDecision(
      page,
      row.id,
      "approve",
      employee !== "SYN-000025" ? ["POOL_HOLDBACK_RELEASE"] : [],
    );
  await switchIdentity(page, "hr");
  current = await cycle(page, current.id);
  current = await request<Cycle>(
    page,
    `/api/v1/compensation/cycles/${current.id}/lock`,
    { reason: "Lock independently signed synthetic awards" },
    current.version,
  );
  expect(current.state).toBe("LOCKED");
  await expect
    .poll(async () => (await cycle(page, current.id)).export_ready, {
      timeout: 30000,
    })
    .toBe(true);
  const planning = await request<PlanningOptions>(
    page,
    "/api/v1/scenarios/options",
  );
  const policy = planning.policies[0];
  expect(policy).toBeDefined();
  let scenario = await request<Scenario>(page, "/api/v1/scenarios", {
    title: `${label} leap-day plan`,
    base_snapshot_id: snapshot!.id,
    policy_id: policy.id,
    fx_set_id: fx!.id,
    reviewer_id: "finance",
    year: 2028,
    planning_date: "2027-12-01",
    scope_orgs: Array.from(
      { length: 12 },
      (_, index) => `D${String(index + 1).padStart(2, "0")}`,
    ),
    budget_minor: "99999999999999999999",
  });
  expect(scenario.annual_total_minor).toBe("77714704176");
  scenario = await request<Scenario>(
    page,
    `/api/v1/scenarios/${scenario.id}/operations`,
    {
      reason: "Cost a genuine synthetic leap-day hire",
      operations: [
        {
          id: randomUUID(),
          kind: "hire",
          effective_date: "2028-02-29",
          reason: "Synthetic leap-day hire",
          employee_id: "SYN-900001",
          position_id: "POS-900001",
          org_id: "D01",
          manager_id: null,
          grade: "L1",
          annual_pay_minor: "12000000",
          currency: "USD",
          fte: "1.0000",
        },
      ],
    },
    scenario.version,
  );
  expect(scenario.annual_total_minor).toBe("77728752452");
  await expect
    .poll(
      async () =>
        (await request<Scenario>(page, `/api/v1/scenarios/${scenario.id}`))
          .verified,
      { timeout: 30000 },
    )
    .toBe(true);
  const revision = scenario.revisions.find((row) => row.number === 2);
  expect(revision).toBeDefined();
  return {
    cycleId: current.id,
    snapshotId: snapshot!.id,
    scenarioId: scenario.id,
    revisionId: revision!.id,
    cycleLabel: label,
    scenarioLabel: `${label} leap-day plan · revision 2`,
    fxId: fx!.id,
    policyId: policy.id,
  };
}
