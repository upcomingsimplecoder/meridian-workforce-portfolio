import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { signIn, chooseHistoricalSource } from "./helpers";

async function lab(page: Page, persona: string) {
  await signIn(page, persona);
  // Direct navigation keeps restricted-route coverage independent of sidebar
  // visibility, which is governed by the current server grants.
  await page.goto("/scenarios");
  await expect(
    page.getByRole("heading", { name: "Scenario Lab", exact: true }),
  ).toBeVisible();
}
async function create(
  page: Page,
  title: string,
  all = true,
  reviewer = "finance",
) {
  await page.getByRole("button", { name: "New plan", exact: true }).click();
  await page.getByLabel("Plan title", { exact: true }).fill(title);
  await chooseHistoricalSource(page, "Base snapshot");
  await page
    .getByLabel("FX version")
    .selectOption({ label: "Synthetic USD planning fixture" });
  await page.getByLabel("Assigned reviewer").selectOption(reviewer);
  await page
    .getByLabel("Budget cap in reporting minor units")
    .fill("99999999999999999999");
  if (all)
    await page.getByRole("button", { name: "Select all departments" }).click();
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/api/v1/scenarios" &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Create pinned plan" }).click(),
  ]);
  expect(response.status()).toBe(201);
  await expect(
    page.getByRole("heading", { name: title, exact: true }),
  ).toBeVisible();
  return response.json();
}
async function fork(page: Page, title: string, reviewer = "finance") {
  await page.getByRole("button", { name: "Fork plan" }).click();
  await page.getByLabel("Branch title").fill(title);
  await page.getByLabel("Branch reviewer").selectOption(reviewer);
  await page
    .getByLabel("Branch reason")
    .fill("Preserve an independent synthetic option");
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/branches") &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Create branch", exact: true }).click(),
  ]);
  expect(response.status()).toBe(201);
  await expect(
    page.getByRole("heading", { name: title, exact: true }),
  ).toBeVisible();
  return response.json();
}
async function selectPlan(page: Page, title: string) {
  const button = page.getByRole("button", { name: title, exact: true });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(button).toHaveAttribute("aria-pressed", "true");
  await expect(
    page
      .getByRole("region", { name: "Selected plan", exact: true })
      .getByRole("heading", { name: title, exact: true }),
  ).toBeVisible();
}
async function action(
  page: Page,
  label: string,
  suffix: string,
  reason: string,
) {
  await page.getByLabel("Decision reason", { exact: true }).fill(reason);
  const button = page.getByRole("button", { name: label, exact: true });
  await expect(button).toBeEnabled({ timeout: 20000 });
  await button.focus();
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith(suffix) &&
        r.request().method() === "POST",
    ),
    button.press("Enter"),
  ]);
  expect(response.status()).toBe(200);
  return response.json();
}

test("Finance and HR branch, calculate, return, revise, approve and promote real persisted plans", async ({
  page,
  browser,
}, info) => {
  await lab(page, "finance");
  test.setTimeout(180000);
  const title = `Workforce plan ${info.project.name} ${randomUUID().slice(0, 8)}`;
  const base = await create(page, title, true, "hr");
  await expect(page.getByTestId("annual-cost")).toHaveText(
    "USD 777,147,041.76",
  );
  const a = await fork(page, title + " option A", "hr");
  await selectPlan(page, title);
  const b = await fork(page, title + " option B", "hr");
  await selectPlan(page, title + " option A");
  await page.getByLabel("Effective date").fill("2028-02-29");
  await page.getByLabel("Synthetic employee ID").fill("SYN-900001");
  await page.getByLabel("Synthetic position ID").fill("POS-900001");
  await page.getByLabel("Annual pay in original minor units").fill("12000000");
  await page
    .getByLabel("Operation reason")
    .fill("Synthetic leap-day hiring rationale");
  const [saved] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/operations") &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Save operation", exact: true }).click(),
  ]);
  expect(saved.status()).toBe(200);
  expect((await saved.json()).id).toBe(a.id);
  await expect(page.getByTestId("annual-delta")).toHaveText("USD 140,482.76");
  await expect(page.getByTestId("annual-cost")).toHaveText(
    "USD 777,287,524.52",
  );
  await page.reload();
  await selectPlan(page, title + " option B");
  await expect(page.getByTestId("annual-delta")).toHaveText("USD 0.00");
  await selectPlan(page, title + " option A");
  await expect(page.getByTestId("annual-delta")).toHaveText("USD 140,482.76");
  for (const name of [title, title + " option A", title + " option B"])
    await page.getByLabel(`Compare ${name}`, { exact: true }).check();
  const comparison = page.getByRole("region", { name: "Branch comparison" });
  await expect(
    comparison.getByRole("cell", { name: "USD 140,482.76", exact: true }),
  ).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  const monthlyTable = page.getByRole("region", {
    name: "Monthly forecast table",
  });
  await monthlyTable.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => monthlyTable.evaluate((el) => el.scrollLeft))
    .toBeGreaterThan(0);
  await monthlyTable.evaluate((el) => {
    el.scrollLeft = 0;
  });
  await page
    .getByRole("region", { name: "Selected plan" })
    .evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.screenshot({
    path: `docs/evidence/p04-${info.project.name}-forecast.png`,
  });
  const submitted = await action(
    page,
    "Submit for review",
    "/submit",
    "Submit option A for funding review",
  );
  await expect(page.getByTestId("plan-state")).toHaveText("SUBMITTED");
  await expect(
    page.getByRole("button", { name: "Save operation", exact: true }),
  ).toHaveCount(0);
  const context = await browser.newContext({
    baseURL: "http://127.0.0.1:3017",
    viewport: { width: 1440, height: 1050 },
  });
  const reviewer = await context.newPage();
  await lab(reviewer, "hr");
  test.setTimeout(180000);
  await reviewer
    .getByRole("button", { name: title + " option A", exact: true })
    .click();
  await action(
    reviewer,
    "Start review",
    "/review",
    "Review the pinned financial assumptions",
  );
  await action(
    reviewer,
    "Return proposal",
    "/decisions",
    "Reconsider the proposed hire before approval",
  );
  await page.getByRole("button", { name: "Refresh plan", exact: true }).click();
  await expect(page.getByTestId("plan-state")).toHaveText("REJECTED");
  await page
    .getByRole("region", { name: "Selected plan" })
    .evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.screenshot({
    path: `docs/evidence/p04-${info.project.name}-returned.png`,
  });
  const revised = await action(
    page,
    "Undo as new revision",
    "/undo",
    "Remove the hire and preserve the prior revision",
  );
  await expect(page.getByTestId("annual-delta")).toHaveText("USD 0.00");
  await page.getByLabel("Forecast revision", { exact: true }).selectOption("2");
  await expect(page.getByTestId("annual-delta")).toHaveText("USD 140,482.76");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", { name: "Export scoped CSV", exact: true })
      .click(),
  ]);
  const csv = await readFile((await download.path())!, "utf8");
  expect(csv.split(/\r?\n/).filter(Boolean)).toHaveLength(145);
  expect(csv).toContain("revision,forecast_sha256");
  expect(csv).not.toContain("SYN-900001");
  await page.getByLabel("Forecast revision", { exact: true }).selectOption("3");
  await action(
    page,
    "Submit for review",
    "/submit",
    "Submit the revised funding plan",
  );
  await reviewer
    .getByRole("button", { name: "Refresh plan", exact: true })
    .click();
  await expect(reviewer.getByTestId("plan-state")).toHaveText("SUBMITTED");
  await action(
    reviewer,
    "Start review",
    "/review",
    "Review the returned proposal revision",
  );
  await action(
    reviewer,
    "Approve plan",
    "/decisions",
    "Approve the revised source-based funding plan",
  );
  const promoted = await action(
    reviewer,
    "Promote planning baseline",
    "/promote",
    "Promote the approved immutable planning baseline",
  );
  await expect(reviewer.getByTestId("plan-state")).toHaveText("PROMOTED");
  const manifest = await reviewer.request.get(
    `/api/v1/scenarios/${a.id}/manifest`,
  );
  expect(manifest.status()).toBe(200);
  const approved = await manifest.json();
  expect(approved.document.approval.reason).toBe(
    "Approve the revised source-based funding plan",
  );
  expect(approved.document.forecast.result.annual.total_minor).toBe(
    "77714704176",
  );
  expect(
    (
      await new AxeBuilder({ page: reviewer })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await reviewer
    .getByRole("region", { name: "Selected plan" })
    .evaluate((el) => el.scrollIntoView({ block: "start" }));
  await reviewer.screenshot({
    path: `docs/evidence/p04-${info.project.name}-promoted.png`,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Refresh plan", exact: true }).click();
  await expect(page.getByTestId("plan-state")).toHaveText("PROMOTED");
  await expect(
    page.getByRole("button", { name: "Save operation", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page
    .getByRole("region", { name: "Selected plan" })
    .evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.screenshot({
    path: `docs/evidence/p04-${info.project.name}-mobile.png`,
  });
  await writeFile(
    `docs/evidence/p04-${info.project.name}-journey.json`,
    JSON.stringify(
      {
        synthetic_only: true,
        browser: info.project.name,
        base_id: base.id,
        branch_a_id: a.id,
        branch_b_id: b.id,
        submitted_version: submitted.version,
        returned_revision: revised.revision,
        promoted_version: promoted.version,
        baseline_total_minor: "77714704176",
        hire_delta_minor: "14048276",
        promoted_total_minor:
          approved.document.forecast.result.annual.total_minor,
        manifest_sha256: approved.sha256,
        source_batch_id: approved.document.source_batch_id,
      },
      null,
      2,
    ) + "\n",
  );
  await context.close();
});

test("real concurrent commands commit once and executive and manager cannot retrieve scenario data", async ({
  page,
}, info) => {
  await lab(page, "hr");
  const scenario = await create(
    page,
    `Workforce plan conflict ${info.project.name} ${randomUUID().slice(0, 8)}`,
    false,
  );
  const me = await (await page.request.get("/api/v1/me")).json();
  const responses = await Promise.all(
    [1, 2].map((n) =>
      page.request.post(`/api/v1/scenarios/${scenario.id}/operations`, {
        headers: {
          Origin: "http://127.0.0.1:3017",
          "X-CSRF-Token": me.csrf_token,
          "If-Match": String(scenario.version),
          "Idempotency-Key": randomUUID(),
        },
        data: {
          reason: "Check concurrent immutable revisions",
          operations: [
            {
              kind: "hire",
              id: randomUUID(),
              effective_date: "2028-07-01",
              reason: "Synthetic concurrent hire",
              employee_id: `SYN-90000${n}`,
              position_id: `POS-90000${n}`,
              org_id: "D01",
              manager_id: null,
              grade: "L1",
              annual_pay_minor: "12000000",
              currency: "USD",
              fte: "1.0000",
            },
          ],
        },
      }),
    ),
  );
  expect(responses.map((r) => r.status()).sort()).toEqual([200, 409]);
  const persisted = await (
    await page.request.get(`/api/v1/scenarios/${scenario.id}`)
  ).json();
  expect(persisted.revision).toBe(2);
  expect(persisted.operations).toHaveLength(1);
  for (const persona of ["executive", "manager-a"]) {
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(
      page.getByRole("link", { name: /Sign in to Meridian/ }),
    ).toBeVisible();
    await lab(page, persona);
    await expect(
      page.getByRole("heading", { name: "Planning access is restricted." }),
    ).toBeVisible();
    for (const route of ["", "/forecast", "/export", "/manifest"]) {
      const denied = await page.request.get(
        `/api/v1/scenarios/${scenario.id}${route}`,
      );
      expect(denied.status()).toBe(403);
      expect(await denied.text()).not.toContain("annual_pay");
      expect(await denied.text()).not.toContain("12000000");
    }
    expect(
      (
        await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
  }
  await page.screenshot({
    path: `docs/evidence/p04-${info.project.name}-restricted.png`,
  });
});
