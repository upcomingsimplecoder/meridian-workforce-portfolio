import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { pauseWorker } from "./runtime-control";
import { signIn, chooseHistoricalSource } from "./helpers";

const root = "/api/v1/compensation";
async function studio(page: Page, persona: string, title?: string) {
  page.setDefaultTimeout(20000);
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
  test.setTimeout(240000);
  // Restricted personas may not receive a Compensation link in the sidebar.
  await page.goto("/compensation");
  await expect(
    page.getByRole("heading", { name: "Compensation Studio" }),
  ).toBeVisible();
  if (title) await openCycle(page, title);
}
async function openCycle(page: Page, title: string) {
  await page.getByLabel("Search cycles", { exact: true }).fill(title);
  await page.locator(".comp-cycle-link").filter({ hasText: title }).click();
  await expect(
    page.getByRole("heading", { name: title, exact: true }),
  ).toBeVisible();
}
async function create(page: Page, title: string) {
  await page.getByRole("button", { name: "New compensation cycle" }).click();
  await page.getByLabel("Cycle title", { exact: true }).fill(title);
  await chooseHistoricalSource(page, "Workforce snapshot");
  await page
    .getByLabel("Guideline version")
    .selectOption({ label: "Synthetic merit and bonus guideline v1" });
  await page
    .getByLabel("Reporting FX version")
    .selectOption({ label: "Synthetic USD planning fixture" });
  for (const [label, value] of [
    ["Eligibility date", "2028-03-31"],
    ["Merit effective date", "2028-05-01"],
    ["Bonus service start", "2028-01-01"],
    ["Bonus service end (exclusive)", "2029-01-01"],
    ["Bonus payment date", "2029-01-15"],
  ])
    await page.getByLabel(label, { exact: true }).fill(value);
  await page
    .getByLabel("Selected synthetic employee IDs")
    .fill("SYN-000025, SYN-000049, SYN-000073");
  await page.getByLabel("Manager 1", { exact: true }).selectOption("manager-a");
  await page.getByLabel("Department 1", { exact: true }).fill("D01");
  await page.getByLabel("Budget 1", { exact: true }).fill("20000.00");
  await page.getByLabel("Holdback 1", { exact: true }).fill("0.00");
  await page.getByRole("button", { name: "Add manager allocation" }).click();
  await page.getByLabel("Manager 2", { exact: true }).selectOption("manager-b");
  await page.getByLabel("Department 2", { exact: true }).fill("D01");
  await page.getByLabel("Budget 2", { exact: true }).fill("5000.00");
  await page.getByLabel("Holdback 2", { exact: true }).fill("1000.00");
  await page.getByLabel("Review department 1", { exact: true }).fill("D01");
  await page
    .getByLabel("Division reviewer 1", { exact: true })
    .selectOption("approver");
  await page.getByLabel("HR signer 1", { exact: true }).selectOption("hr");
  await page
    .getByLabel("Finance signer 1", { exact: true })
    .selectOption("finance");
  await page
    .getByLabel("Configuration reason")
    .fill("Synthetic independently governed compensation review");
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === root + "/cycles" &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", { name: "Save cycle configuration", exact: true })
      .click(),
  ]);
  expect(response.status()).toBe(200);
  const cycle = await response.json();
  await expect(
    page.getByRole("heading", { name: title, exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Cycle action reason")
    .fill("Launch the frozen synthetic roster and review assignments");
  const [launched] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/launch") &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Launch frozen cycle" }).click(),
  ]);
  expect(launched.status()).toBe(200);
  return { ...cycle, ...(await launched.json()) };
}
function row(page: Page, employee: string) {
  return page.locator(".comp-grid tbody tr").filter({ hasText: employee });
}
async function saveAward(
  page: Page,
  employee: string,
  merit: string,
  bonus = "1000.00",
) {
  await page
    .getByLabel(`Merit amount ${employee}`, { exact: true })
    .fill(merit);
  await page
    .getByLabel(`Bonus target ${employee}`, { exact: true })
    .fill(bonus);
  await page
    .getByLabel(`Award rationale ${employee}`, { exact: true })
    .fill("Synthetic manager proposal with documented rationale");
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/award") &&
        r.request().method() === "POST",
    ),
    row(page, employee)
      .getByRole("button", { name: "Save award", exact: true })
      .click(),
  ]);
  expect(response.status()).toBe(200);
  const proposal = await response.json();
  await expect(
    row(page, employee).getByRole("button", {
      name: "Submit proposal",
      exact: true,
    }),
  ).toBeEnabled({ timeout: 20000 });
  return proposal;
}
async function submit(page: Page, employee: string) {
  const button = row(page, employee).getByRole("button", {
    name: "Submit proposal",
    exact: true,
  });
  await button.focus();
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/submit") &&
        r.request().method() === "POST",
    ),
    button.press("Enter"),
  ]);
  expect(response.status()).toBe(200);
  return response.json();
}
async function inspect(page: Page, employee: string) {
  const proposalRow = row(page, employee);
  const employeeName = await proposalRow
    .locator(".comp-person-link")
    .innerText();
  await proposalRow
    .getByRole("button", { name: "Review details", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Proposal decisions and provenance" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: `${employeeName} · ${employee}`,
      exact: true,
    }),
  ).toBeVisible();
}
async function review(
  page: Page,
  action: "start" | "approve" | "return",
  exceptions: string[] = [],
) {
  if (action !== "start")
    await page.getByLabel("Decision", { exact: true }).selectOption(action);
  await page
    .getByLabel("Decision reason", { exact: true })
    .fill(
      action === "return"
        ? "Return for a lower merit award and revised justification"
        : "Independently reviewed this exact saved revision and its inputs",
    );
  for (const exception of exceptions)
    await page
      .getByLabel(exception.replaceAll("_", " "), { exact: true })
      .check();
  const button = page.getByRole("button", {
    name:
      action === "start"
        ? "Begin assigned review"
        : action === "approve"
          ? "Approve current revision"
          : "Return current revision",
    exact: true,
  });
  await button.focus();
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith(
          action === "start" ? "/review" : "/decisions",
        ) && r.request().method() === "POST",
    ),
    button.press("Enter"),
  ]);
  expect(response.status()).toBe(200);
  return response.json();
}

test("compensation runs through manager return/revision, three signatures, locked golden export and auditor evidence", async ({
  page,
}, info) => {
  await studio(page, "hr");
  const title = `Compensation review ${info.project.name} ${randomUUID().slice(0, 8)}`;
  const cycle = await create(page, title);
  const identifiers: Record<string, string> = {};
  await studio(page, "manager-a", title);
  await expect(page.locator(".comp-grid tbody tr")).toHaveCount(1);
  identifiers["SYN-000025"] = (
    await saveAward(page, "SYN-000025", "10000.00")
  ).id;
  await submit(page, "SYN-000025");
  await studio(page, "manager-b", title);
  await expect(page.locator(".comp-grid tbody tr")).toHaveCount(2);
  await expect(page.locator(".comp-detail")).not.toContainText("manager-a");
  identifiers["SYN-000049"] = (
    await saveAward(page, "SYN-000049", "1200.00")
  ).id;
  identifiers["SYN-000073"] = (
    await saveAward(page, "SYN-000073", "1200.00")
  ).id;
  await submit(page, "SYN-000049");
  await submit(page, "SYN-000073");
  expect(
    (
      await page.request.get(`${root}/proposals/${identifiers["SYN-000025"]}`)
    ).status(),
  ).toBe(404);
  const table = page.getByRole("region", {
    name: "Authorized compensation proposal table",
  });
  await table.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => table.evaluate((element) => element.scrollLeft))
    .toBeGreaterThan(0);
  await table.evaluate((element) => {
    element.scrollLeft = 0;
  });
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page
    .locator(".comp-detail")
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.screenshot({
    path: `docs/evidence/p05-${info.project.name}-manager-pools.png`,
  });
  await studio(page, "approver", title);
  for (const employee of ["SYN-000025", "SYN-000049", "SYN-000073"]) {
    await inspect(page, employee);
    await review(page, "start");
    await review(
      page,
      "approve",
      employee === "SYN-000025" ? ["MERIT_GUIDELINE_EXCEPTION"] : [],
    );
  }
  await studio(page, "hr", title);
  await inspect(page, "SYN-000049");
  const returned = await review(page, "return");
  expect(returned.state).toBe("RETURNED");
  expect(
    returned.steps.every((step: { approved: boolean }) => !step.approved),
  ).toBe(true);
  await page.screenshot({
    path: `docs/evidence/p05-${info.project.name}-returned.png`,
  });
  await studio(page, "manager-b", title);
  const revised = await saveAward(page, "SYN-000049", "1000.00");
  expect(revised.revision).toBeGreaterThan(returned.revision);
  await page.reload();
  await openCycle(page, title);
  await expect(
    page.getByLabel("Merit amount SYN-000049", { exact: true }),
  ).toHaveValue("1000.00");
  await submit(page, "SYN-000049");
  await studio(page, "approver", title);
  await inspect(page, "SYN-000049");
  await review(page, "start");
  await review(page, "approve");
  await studio(page, "hr", title);
  for (const employee of ["SYN-000025", "SYN-000049", "SYN-000073"]) {
    await inspect(page, employee);
    await review(page, "approve");
  }
  await studio(page, "finance", title);
  for (const employee of ["SYN-000025", "SYN-000049", "SYN-000073"]) {
    await inspect(page, employee);
    await review(
      page,
      "approve",
      employee === "SYN-000025" ? [] : ["POOL_HOLDBACK_RELEASE"],
    );
  }
  await expect(
    page.getByText("Signed for this revision", { exact: true }),
  ).toHaveCount(3);
  await page.screenshot({
    path: `docs/evidence/p05-${info.project.name}-finance-signatures.png`,
  });
  await studio(page, "hr", title);
  await page
    .getByLabel("Cycle action reason")
    .fill("Lock all independently approved current proposal revisions");
  const [locked] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/lock") &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Lock approved cycle" }).click(),
  ]);
  expect(locked.status()).toBe(200);
  await expect(
    page.getByRole("button", {
      name: "Download compensation CSV",
      exact: true,
    }),
  ).toBeVisible({ timeout: 20000 });
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", { name: "Download compensation CSV", exact: true })
      .click(),
  ]);
  const csv = await readFile((await download.path())!, "utf8");
  const lines = csv.trim().split("\n");
  expect(lines).toHaveLength(4);
  expect(lines[1].split(",").slice(0, 17)).toEqual([
    "SYN-000025",
    "D01",
    "CC-001",
    "USD",
    "2",
    "2028-05-01",
    "12300000",
    "1000000",
    "13300000",
    "1",
    "1000000",
    "666664",
    "2029-01-15",
    "100000",
    "100000",
    "1100000",
    "2",
  ]);
  expect(lines[2].split(",").slice(0, 16)).toEqual([
    "SYN-000049",
    "D01",
    "CC-001",
    "USD",
    "2",
    "2028-05-01",
    "9700000",
    "100000",
    "9800000",
    "1",
    "100000",
    "66664",
    "2029-01-15",
    "100000",
    "100000",
    "200000",
  ]);
  expect(lines[3].split(",").slice(0, 16)).toEqual([
    "SYN-000073",
    "D01",
    "CC-001",
    "USD",
    "2",
    "2028-05-01",
    "17700000",
    "120000",
    "17820000",
    "1",
    "120000",
    "80000",
    "2029-01-15",
    "100000",
    "100000",
    "220000",
  ]);
  await page
    .getByLabel("Cycle action reason")
    .fill("Reconcile the actual signed local files with the locked source");
  const [reconciled] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/reconcile") &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Reconcile local export" }).click(),
  ]);
  expect(reconciled.status()).toBe(200);
  await expect(
    page.getByText(
      "Local export reconciled against immutable source revisions.",
    ),
  ).toBeVisible();
  await page.screenshot({
    path: `docs/evidence/p05-${info.project.name}-reconciled.png`,
  });
  await studio(page, "manager-b", title);
  await expect(
    page.getByLabel("Merit amount SYN-000049", { exact: true }),
  ).toHaveCount(0);
  const me = await (await page.request.get("/api/v1/me")).json();
  const proposal = await (
    await page.request.get(`${root}/proposals/${identifiers["SYN-000049"]}`)
  ).json();
  const forbidden = await page.request.post(
    `${root}/proposals/${proposal.id}/award`,
    {
      data: {
        merit_minor: "1",
        bonus_minor: "1",
        comment: "Forbidden locked edit",
        reason: "Forbidden locked edit",
      },
      headers: {
        Origin: "http://127.0.0.1:3017",
        "X-CSRF-Token": me.csrf_token,
        "If-Match": String(proposal.version),
        "Idempotency-Key": randomUUID(),
      },
    },
  );
  expect(forbidden.status()).toBe(409);
  await studio(page, "auditor");
  const evidence = page
    .locator(".comp-evidence article")
    .filter({ hasText: cycle.id });
  const nextEvidence = page.getByRole("button", { name: "Next evidence page" });
  await expect(nextEvidence).toBeVisible();
  for (let pages = 0; pages < 100 && !(await evidence.count()); pages++) {
    if (!(await nextEvidence.isEnabled())) break;
    const [response] = await Promise.all([
      page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === root + "/evidence" &&
          r.request().method() === "GET",
      ),
      nextEvidence.click(),
    ]);
    expect(response.status()).toBe(200);
    const visiblePage = await response.json();
    // Wait for this exact response to reach the DOM before inspecting the page or
    // advancing its cursor. A response alone does not mean React rendered it.
    if (visiblePage.items.length)
      await expect(
        page.locator(".comp-evidence article").first(),
      ).toContainText(visiblePage.items[0].cycle_id);
    else
      await expect(
        page.getByText(
          "No compensation evidence exists in your current scope.",
        ),
      ).toBeVisible();
    await expect(nextEvidence).toBeVisible();
  }
  await expect(evidence).toContainText("USD pool charge USD 15,200.00");
  await expect(evidence).toContainText("Local manifest signature verified");
  await expect(evidence).toContainText("Source/file reconciliation matched");
  await expect(evidence).not.toContainText("SYN-");
  expect(
    (await page.request.get(`${root}/cycles/${cycle.id}/proposals`)).status(),
  ).toBe(403);
  expect(
    (await page.request.get(`${root}/cycles/${cycle.id}/export`)).status(),
  ).toBe(403);
  await evidence.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: `docs/evidence/p05-${info.project.name}-auditor.png`,
  });
  await writeFile(
    `docs/evidence/p05-${info.project.name}-journey.json`,
    JSON.stringify(
      {
        cycle_id: cycle.id,
        state: "RECONCILED",
        row_count: 3,
        totals_by_currency: {
          USD: {
            annualized_merit_minor: "1220000",
            merit_first_year_minor: "813328",
            bonus_payable_minor: "300000",
            pool_charge_minor: "1520000",
          },
        },
        source: "real persisted local API/worker/browser journey",
        synthetic_only: true,
      },
      null,
      2,
    ) + "\n",
  );
});

test("worker outage, exact autosave, stale concurrent writes, scoped negatives and mobile compensation", async ({
  page,
  browser,
}, info) => {
  await studio(page, "hr");
  const title = `Compensation review controls ${info.project.name} ${randomUUID().slice(0, 8)}`;
  const cycle = await create(page, title);
  await studio(page, "manager-a", title);
  const resume = await pauseWorker("outbox");
  let proposal;
  try {
    await page.getByLabel("Merit amount SYN-000025").fill("1200.00");
    await page.getByLabel("Bonus target SYN-000025").fill("1000.00");
    await page
      .getByLabel("Award rationale SYN-000025")
      .fill("Synthetic outage persistence check");
    const [saved] = await Promise.all([
      page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname.endsWith("/award") &&
          r.request().method() === "POST",
      ),
      page.getByLabel("Merit amount SYN-000025").press("Enter"),
    ]);
    expect(saved.status()).toBe(200);
    proposal = await saved.json();
    await expect(row(page, "SYN-000025")).toContainText(
      "Saved · awaiting worker verification",
    );
    await expect(
      row(page, "SYN-000025").getByRole("button", { name: "Submit proposal" }),
    ).toBeDisabled();
    await page.reload();
    await openCycle(page, title);
    await expect(page.getByLabel("Merit amount SYN-000025")).toHaveValue(
      "1200.00",
    );
    await page.screenshot({
      path: `docs/evidence/p05-${info.project.name}-worker-outage.png`,
    });
  } finally {
    await resume();
  }
  await expect(
    row(page, "SYN-000025").getByRole("button", { name: "Submit proposal" }),
  ).toBeEnabled({ timeout: 20000 });
  const me = await (await page.request.get("/api/v1/me")).json();
  const requests = await Promise.all(
    ["100000", "110000"].map((merit_minor) =>
      page.request.post(`${root}/proposals/${proposal.id}/award`, {
        data: {
          merit_minor,
          bonus_minor: "100000",
          comment: "Synthetic simultaneous saved award",
          reason: "Concurrent optimistic locking check",
        },
        headers: {
          Origin: "http://127.0.0.1:3017",
          "X-CSRF-Token": me.csrf_token,
          "If-Match": String(proposal.version),
          "Idempotency-Key": randomUUID(),
        },
      }),
    ),
  );
  expect(requests.map((response) => response.status()).sort()).toEqual([
    200, 409,
  ]);
  const acknowledged = await requests
    .find((response) => response.status() === 200)!
    .json();
  const self = await page.request.post(
    `${root}/proposals/${proposal.id}/decisions`,
    {
      data: {
        revision_id: acknowledged.revision_id,
        decision: "approve",
        reason: "Forbidden self approval",
        acknowledged_exceptions: [],
      },
      headers: {
        Origin: "http://127.0.0.1:3017",
        "X-CSRF-Token": me.csrf_token,
        "If-Match": String(acknowledged.version),
        "Idempotency-Key": randomUUID(),
      },
    },
  );
  expect(self.status()).toBe(403);
  await page.getByRole("button", { name: "Refresh cycle" }).click();
  await expect(page.getByLabel("Merit amount SYN-000025")).toHaveValue(
    acknowledged.award.merit_minor === "100000" ? "1000.00" : "1100.00",
  );
  await expect(
    row(page, "SYN-000025").getByRole("button", { name: "Submit proposal" }),
  ).toBeEnabled({ timeout: 20000 });
  const reviewerContext = await browser.newContext({
    baseURL: "http://127.0.0.1:3017",
  });
  try {
    const reviewer = await reviewerContext.newPage();
    await studio(reviewer, "approver", title);
    const reviewerMe = await (await reviewer.request.get("/api/v1/me")).json();
    const races = await Promise.all([
      page.request.post(`${root}/proposals/${proposal.id}/submit`, {
        data: { reason: "Concurrent verified manager submission" },
        headers: {
          Origin: "http://127.0.0.1:3017",
          "X-CSRF-Token": me.csrf_token,
          "If-Match": String(acknowledged.version),
          "Idempotency-Key": randomUUID(),
        },
      }),
      reviewer.request.post(`${root}/proposals/${proposal.id}/review`, {
        data: {
          revision_id: acknowledged.revision_id,
          reason: "Concurrent named approver start",
        },
        headers: {
          Origin: "http://127.0.0.1:3017",
          "X-CSRF-Token": reviewerMe.csrf_token,
          "If-Match": String(acknowledged.version),
          "Idempotency-Key": randomUUID(),
        },
      }),
    ]);
    expect(races.map((response) => response.status())).toEqual([200, 409]);
    await info.attach("compensation-concurrent-actors", {
      body: JSON.stringify({
        manager_submit: 200,
        stale_approver_start: 409,
        saved_revision: acknowledged.revision,
      }),
      contentType: "application/json",
    });
  } finally {
    await reviewerContext.close();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `docs/evidence/p05-${info.project.name}-mobile.png`,
  });
  await page.setViewportSize({ width: 1440, height: 1050 });
  await studio(page, "executive");
  await expect(
    page.getByRole("heading", { name: "Compensation access is restricted." }),
  ).toBeVisible();
  for (const path of [
    "/cycles",
    `/cycles/${cycle.id}`,
    `/proposals/${proposal.id}`,
    "/evidence",
  ])
    expect((await page.request.get(root + path)).status()).toBe(403);
});
