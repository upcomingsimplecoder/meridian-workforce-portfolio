import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import {
  pauseWorker,
  createAnalyticalBarrier,
  readBarrier,
  releaseBarrier,
  removeBarrier,
  startAnalyticalWorker,
  containerRuntime,
} from "./runtime-control";
import type { components } from "../../packages/contracts/api";
import { measureAnalyticalReads } from "./analytics-performance";
import {
  createReviewedBusiness,
  request,
  switchIdentity,
} from "./analytics-business";
type Run = components["schemas"]["AnalyticsRunView"];
async function consolePage(page: Page) {
  await page.getByRole("link", { name: "Analytics", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Analytics & reconciliation" }),
  ).toBeVisible();
}
async function newRun(
  page: Page,
  choices?: { cycleLabel: string; scenarioLabel: string },
) {
  await page.getByRole("button", { name: "New analytical run" }).click();
  await page
    .getByLabel("Search published sources", { exact: true })
    .fill("June workforce");
  await page.getByRole("button", { name: "Find source", exact: true }).click();
  await expect(
    page
      .getByLabel("Published source", { exact: true })
      .locator("option", { hasText: "June workforce · corrected" }),
  ).toHaveCount(1);
  await page
    .getByLabel("Published source", { exact: true })
    .selectOption({ label: "June workforce · corrected" });
  if (choices) {
    await page
      .getByLabel(`Include cycle ${choices.cycleLabel}`, { exact: true })
      .check();
    await page
      .getByLabel(`Include scenario ${choices.scenarioLabel}`, { exact: true })
      .check();
  }
  await page
    .getByLabel("Analytical purpose", { exact: true })
    .fill(
      "Reconcile exact immutable source, monetary manifest and scenario inputs",
    );
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/api/v1/analytics/runs" &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", { name: "Request reconciled run", exact: true })
      .click(),
  ]);
  expect(response.status()).toBe(201);
  const run: Run = await response.json();
  await expect(
    page.getByRole("region", { name: "Selected analytical run" }),
  ).toHaveAttribute("data-run-id", run.id);
  return run;
}
async function ready(page: Page, id: string, status: "PASS" | "DRIFT") {
  const region = page.getByRole("region", { name: "Selected analytical run" });
  await expect(region).toHaveAttribute("data-run-id", id);
  await expect(region.getByRole("status")).toContainText(status, {
    timeout: 45000,
  });
  await expect(
    page.locator(`.analytical-run-link[data-run-id="${id}"]`),
  ).toContainText(status);
  return request<Run>(page, `/api/v1/analytics/runs/${id}`);
}
test("governed SQL totals, actual missed delivery and immutable repair survive browser reload and analytics outage", async ({
  page,
}, info) => {
  page.setDefaultTimeout(25000);
  const label = `Analytical review ${info.project.name} ${randomUUID().slice(0, 8)}`;
  const business = await createReviewedBusiness(page, label);
  await consolePage(page);
  const requested = await newRun(page, business);
  const normal = await ready(page, requested.id, "PASS");
  expect(normal.headcount).toBe(5000);
  expect(normal.fte).toBe("4900");
  expect(normal.employee_versions).toBe(5002);
  expect(normal.compensation[0]).toMatchObject({
    row_count: 3,
    annualized_merit_minor: "1220000",
    merit_first_year_minor: "813328",
    bonus_payable_minor: "300000",
    pool_charge_minor: "1520000",
    remaining_minor: "880000",
  });
  expect(normal.scenarios[0]).toMatchObject({
    annual_minor: "77728752452",
    baseline_minor: "77714704176",
    delta_minor: "14048276",
    fx_set_id: business.fxId,
  });
  expect(normal.comparisons.every((row) => row.matches)).toBe(true);
  await measureAnalyticalReads(page, normal, info.project.name);
  const selected = page.getByRole("region", {
    name: "Selected analytical run",
  });
  const compensation = selected.getByRole("region", {
    name: "Compensation SQL totals",
  });
  await expect(compensation).toContainText("12,200.00");
  await expect(compensation).toContainText("8,133.28");
  await expect(compensation).toContainText("15,200.00");
  await expect(
    selected.getByRole("region", { name: "Scenario SQL costs" }),
  ).toContainText("140,482.76");
  await page.screenshot({
    path: `docs/evidence/p07-${info.project.name}-reconciled.png`,
    fullPage: true,
  });
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    selected
      .getByRole("button", { name: "Download verified analytical manifest" })
      .click(),
  ]);
  const path = await download.path();
  expect(path).not.toBeNull();
  const originalManifest = await readFile(path!, "utf8");
  expect(JSON.parse(originalManifest).manifest.result_sha256).toBe(
    normal.result_sha256,
  );
  const originalResponse = await page.request.get(
    `/api/v1/analytics/runs/${normal.id}/manifest`,
  );
  expect(originalResponse.status()).toBe(200);
  await selected
    .getByLabel("Operation reason", { exact: true })
    .fill(
      "Simulate the actual locked-cycle delivery missing from a new projection",
    );
  await selected
    .getByLabel("Delivery to omit in a new drill", { exact: true })
    .selectOption(normal.delivery_event_ids[0]);
  const [gapResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/gap-drill") &&
        r.request().method() === "POST",
    ),
    selected.getByRole("button", { name: "Simulate missed delivery" }).click(),
  ]);
  expect(gapResponse.status()).toBe(201);
  const gapId = (await gapResponse.json()).id;
  const gap = await ready(page, gapId, "DRIFT");
  expect(gap.compensation[0].pool_charge_minor).toBe("0");
  expect(gap.compensation[0].row_count).toBe(0);
  expect(
    gap.comparisons.some(
      (row) =>
        row.expected === "1520000" && row.observed === "0" && !row.matches,
    ),
  ).toBe(true);
  await expect(
    selected.getByText("Publication blocked", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: `docs/evidence/p07-${info.project.name}-drift.png`,
    fullPage: true,
  });
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.reload();
  const gapButton = page.locator(
    `.analytical-run-link[data-run-id="${gap.id}"]`,
  );
  await expect(gapButton).toBeEnabled();
  await gapButton.click();
  await expect(selected.getByRole("status")).toContainText("DRIFT");
  await selected
    .getByLabel("Operation reason", { exact: true })
    .fill("Repair the exact missing event with the original pinned versions");
  const [repairResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/repair") &&
        r.request().method() === "POST",
    ),
    selected
      .getByRole("button", { name: "Repair exact pinned inputs" })
      .click(),
  ]);
  expect(repairResponse.status()).toBe(201);
  const repairId = (await repairResponse.json()).id;
  const repair = await ready(page, repairId, "PASS");
  expect(repair.result_sha256).toBe(normal.result_sha256);
  expect(repair.inputs_sha256).toBe(normal.inputs_sha256);
  expect(repair.artifacts).toEqual(normal.artifacts);
  expect(
    await (
      await page.request.get(`/api/v1/analytics/runs/${normal.id}/manifest`)
    ).text(),
  ).toBe(await originalResponse.text());
  await page.screenshot({
    path: `docs/evidence/p07-${info.project.name}-repair.png`,
    fullPage: true,
  });
  let queued: Run;
  const resume = await pauseWorker("analytics");
  try {
    queued = await newRun(page);
    expect(
      (await request<Run>(page, `/api/v1/analytics/runs/${queued.id}`)).status,
    ).toBe("QUEUED");
    expect(
      (await request<Run>(page, `/api/v1/analytics/runs/${queued.id}`))
        .headcount,
    ).toBeNull();
    await page.reload();
    const button = page.locator(
      `.analytical-run-link[data-run-id="${queued.id}"]`,
    );
    await expect(button).toBeEnabled();
    await button.click();
    await expect(selected.getByRole("status")).toContainText("QUEUED");
    const revision = await request<components["schemas"]["ScenarioDetail"]>(
      page,
      `/api/v1/scenarios/${business.scenarioId}`,
    );
    await request(
      page,
      `/api/v1/scenarios/${business.scenarioId}/undo`,
      { reason: "Domain worker remains available during analytical outage" },
      revision.version,
    );
    await expect
      .poll(
        async () =>
          (
            await request<components["schemas"]["ScenarioDetail"]>(
              page,
              `/api/v1/scenarios/${business.scenarioId}`,
            )
          ).verified,
        { timeout: 30000 },
      )
      .toBe(true);
    await page.screenshot({
      path: `docs/evidence/p07-${info.project.name}-worker-pending.png`,
      fullPage: true,
    });
  } finally {
    await resume();
  }
  await ready(page, queued!.id, "PASS");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeHidden();
  // Hold delivery of the actual authorized backend list response. The panel
  // must remain keyboard-scrollable while every row control is disabled.
  const history = page.getByRole("region", { name: "Analytical run history" });
  const delivery = Promise.withResolvers<void>();
  const listPattern = "**/api/v1/analytics/runs?*";
  await page.route(listPattern, async (route) => {
    const response = await route.fetch();
    await delivery.promise;
    await route.fulfill({ response });
  });
  try {
    await page
      .getByRole("button", { name: "Refresh persisted state", exact: true })
      .click();
    await expect(history).toHaveAttribute("aria-busy", "true");
    await expect(
      history.locator(".analytical-run-link").first(),
    ).toBeDisabled();
    await history.focus();
    await expect(history).toBeFocused();
    await history.evaluate((node) => {
      node.scrollTop = 0;
    });
    await page.keyboard.press("ArrowDown");
    await expect
      .poll(() => history.evaluate((node) => node.scrollTop))
      .toBeGreaterThan(0);
    await page.keyboard.press("End");
    await expect
      .poll(() =>
        history.evaluate(
          (node) => node.scrollHeight - node.clientHeight - node.scrollTop,
        ),
      )
      .toBeLessThanOrEqual(1);
    await page.keyboard.press("ArrowUp");
    await expect
      .poll(() =>
        history.evaluate(
          (node) => node.scrollHeight - node.clientHeight - node.scrollTop,
        ),
      )
      .toBeGreaterThan(0);
    await page.keyboard.press("Home");
    await expect.poll(() => history.evaluate((node) => node.scrollTop)).toBe(0);
    await page.keyboard.press("PageDown");
    await expect
      .poll(() => history.evaluate((node) => node.scrollTop))
      .toBeGreaterThan(0);
    await page.keyboard.press("PageUp");
    await expect.poll(() => history.evaluate((node) => node.scrollTop)).toBe(0);
    expect(
      (
        await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
  } finally {
    delivery.resolve();
    await page.unrouteAll({ behavior: "wait" });
  }
  await expect(history).toHaveAttribute("aria-busy", "false");
  await page
    .getByRole("button", { name: "Open navigation", exact: true })
    .click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  await page.locator(".help-link").scrollIntoViewIfNeeded();
  await expect(page.locator(".help-link")).toBeInViewport();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page
    .getByRole("button", { name: "Close navigation", exact: true })
    .click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeHidden();
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
    path: `docs/evidence/p07-${info.project.name}-mobile.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1050 });
  const normalButton = page.locator(
    `.analytical-run-link[data-run-id="${normal.id}"]`,
  );
  await expect(normalButton).toBeEnabled();
  await normalButton.click();
  await expect(
    selected.getByRole("region", { name: "Compensation SQL totals" }),
  ).toBeVisible();
  const scroll = selected.getByRole("region", {
    name: "Compensation SQL totals",
  });
  await scroll.focus();
  await expect(scroll).toBeFocused();
  await scroll.evaluate((node) => {
    node.scrollLeft = 0;
  });
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => scroll.evaluate((node) => node.scrollLeft))
    .toBeGreaterThan(0);
  await writeFile(
    `docs/evidence/p07-${info.project.name}-journey.json`,
    JSON.stringify(
      {
        claim_scope: containerRuntime
          ? "actual Docker Compose synthetic OIDC/browser/API/SQL journey"
          : "actual native synthetic OIDC/browser/API/SQL journey",
        business,
        normal_run_id: normal.id,
        gap_run_id: gap.id,
        repaired_run_id: repair.id,
        result_sha256: repair.result_sha256,
        inputs_sha256: repair.inputs_sha256,
        expected_headcount: 5000,
        expected_fte: "4900",
        expected_financial_minor: {
          annual_merit: "1220000",
          first_year_merit: "813328",
          bonus: "300000",
          pool_charge: "1520000",
          scenario_delta: "14048276",
        },
        cloud: "UNVERIFIED",
      },
      null,
      2,
    ),
  );
});
test("executive and unrelated manager never retrieve raw analytics; auditor receives redacted evidence", async ({
  page,
}, info) => {
  await switchIdentity(page, "executive");
  await consolePage(page);
  await expect(
    page.getByRole("heading", { name: "Governed workforce totals" }),
  ).toBeVisible();
  await expect(page.getByText("5,000", { exact: true })).toBeVisible();
  for (const path of [
    "/api/v1/analytics/runs",
    "/api/v1/analytics/options",
    "/api/v1/analytics/evidence",
  ]) {
    const response = await page.request.get(path);
    expect(response.status()).toBe(403);
    expect(await response.text()).not.toContain("annualized_merit_minor");
  }
  expect(await page.locator("main").innerText()).not.toContain("SYN-");
  await page.screenshot({
    path: `docs/evidence/p07-${info.project.name}-executive.png`,
    fullPage: false,
  });
  await switchIdentity(page, "manager-b");
  await consolePage(page);
  await expect(
    page.getByRole("heading", { name: "Analytics access is restricted" }),
  ).toBeVisible();
  expect((await page.request.get("/api/v1/analytics/runs")).status()).toBe(403);
  const me: components["schemas"]["Me"] = await (
    await page.request.get("/api/v1/me")
  ).json();
  expect(
    (
      await page.request.post("/api/v1/analytics/runs", {
        data: {
          snapshot_id: "28f7ae3e-f62b-5dbf-98a8-3a6ade9fea37",
          as_of: "2027-06-30",
          reason: "Unauthorized cross-scope test",
        },
        headers: {
          "X-CSRF-Token": me.csrf_token,
          Origin: "http://127.0.0.1:3017",
          "Idempotency-Key": randomUUID(),
        },
      })
    ).status(),
  ).toBe(403);
  await switchIdentity(page, "auditor");
  await consolePage(page);
  await expect(
    page.getByRole("heading", { name: "Redacted analytical evidence" }),
  ).toBeVisible();
  await expect(page.locator(".analytical-evidence").first()).toBeVisible();
  const evidence = await (
    await page.request.get("/api/v1/analytics/evidence")
  ).json();
  expect(evidence.length).toBeGreaterThan(0);
  expect(JSON.stringify(evidence)).not.toContain("compensation");
  expect(JSON.stringify(evidence)).not.toContain("headcount");
  expect(
    (
      await page.request.get(
        `/api/v1/analytics/runs/${evidence[0].id}/manifest`,
      )
    ).status(),
  ).toBe(403);
  expect(await page.locator("main").innerText()).not.toContain("15,200.00");
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: `docs/evidence/p07-${info.project.name}-auditor.png`,
    fullPage: false,
  });
});

test("two independent SQL workers overlap without duplicate delivery or shared catalog writes", async ({
  page,
}, info) => {
  await switchIdentity(page, "hr");
  const processes: ReturnType<typeof startAnalyticalWorker>[] = [];
  const barrier = await createAnalyticalBarrier();
  const resume = await pauseWorker("analytics");
  try {
    const body = {
      snapshot_id: "28f7ae3e-f62b-5dbf-98a8-3a6ade9fea37",
      as_of: "2027-06-30",
      reason: "Verify two genuine independent synthetic analytical deliveries",
    };
    const a = await request<Run>(page, "/api/v1/analytics/runs", body);
    const b = await request<Run>(page, "/api/v1/analytics/runs", body);
    for (let index = 0; index < 2; index++)
      processes.push(startAnalyticalWorker(barrier, [a.id, b.id], index));
    const completions = processes.map(
      ({ child }) =>
        new Promise<number | null>((resolve, reject) => {
          child.once("error", reject);
          child.once("exit", (code) => resolve(code));
        }),
    );
    await expect
      .poll(
        async () =>
          (await readBarrier(barrier, a.id)) === a.id &&
          (await readBarrier(barrier, b.id)) === b.id &&
          processes.every(({ child }) => child.exitCode === null),
        {
          timeout: 30000,
          message:
            "Both independent workers must hold their distinct job locks before the barrier releases",
        },
      )
      .toBe(true);
    expect(await readBarrier(barrier, a.id)).toBe(a.id);
    expect(await readBarrier(barrier, b.id)).toBe(b.id);
    await releaseBarrier(barrier);
    expect(await Promise.all(completions)).toEqual([0, 0]);
    const results = await Promise.all([
      request<Run>(page, `/api/v1/analytics/runs/${a.id}`),
      request<Run>(page, `/api/v1/analytics/runs/${b.id}`),
    ]);
    expect(results.map((row) => row.status)).toEqual(["PASS", "PASS"]);
    expect(results.map((row) => row.artifacts.length)).toEqual([10, 10]);
    expect(results[0].result_sha256).toBe(results[1].result_sha256);
    expect(results[0].inputs_sha256).toBe(results[1].inputs_sha256);
    await writeFile(
      `docs/evidence/p07-${info.project.name}-worker-concurrency.json`,
      JSON.stringify(
        {
          claim_scope: containerRuntime
            ? "actual Docker Compose PostgreSQL SKIP LOCKED / two worker containers / independent dbt catalogs"
            : "actual native PostgreSQL SKIP LOCKED / two Python processes / independent dbt catalogs",
          run_ids: [a.id, b.id],
          overlap_observed_before_either_exit: true,
          process_exit_codes: [0, 0],
          statuses: results.map((row) => row.status),
          artifacts_per_run: results.map((row) => row.artifacts.length),
          report_sha256: results[0].result_sha256,
          cloud: "UNVERIFIED",
        },
        null,
        2,
      ),
    );
  } finally {
    for (const worker of processes) await worker.terminate();
    await resume();
    await removeBarrier(barrier);
  }
});

test("completed analytics visibly reports a real heartbeat outage and recovers without changing reconciled results", async ({
  page,
  browser,
}, info) => {
  await switchIdentity(page, "finance");
  const choices = await request<components["schemas"]["AnalyticsOptions"]>(
    page,
    "/api/v1/analytics/options",
  );
  const source = choices.snapshots[0];
  expect(source).toBeDefined();
  const run = await request<Run>(page, "/api/v1/analytics/runs", {
    snapshot_id: source.id,
    as_of: source.as_of,
    reason: "Verify live freshness of a completed immutable analytical result",
  });
  await expect
    .poll(
      async () =>
        (await request<Run>(page, `/api/v1/analytics/runs/${run.id}`)).status,
      { timeout: 45000 },
    )
    .toBe("PASS");
  const before = await request<Run>(page, `/api/v1/analytics/runs/${run.id}`);
  expect(before.pipeline_status).toBe("CURRENT");
  expect(before.headcount).toBe(5000);
  expect(before.comparisons.every((row) => row.matches)).toBe(true);
  const originalManifest = await (
    await page.request.get(`/api/v1/analytics/runs/${run.id}/manifest`)
  ).body();
  await consolePage(page);
  await page.locator(`.analytical-run-link[data-run-id="${run.id}"]`).click();
  const selected = page.getByRole("region", {
    name: "Selected analytical run",
  });
  await expect(selected.getByRole("status")).toHaveText("PASS · CURRENT");
  const executive = await browser.newPage();
  let resume: (() => Promise<void>) | undefined;
  let offline: Run | undefined;
  try {
    await switchIdentity(executive, "executive");
    await consolePage(executive);
    await expect(executive.getByRole("status")).toHaveText("CURRENT");
    resume = await pauseWorker("analytics");
    // Observe the real 60-second heartbeat boundary; never edit heartbeat rows
    // or fake the clock in the browser/recovery gate.
    await expect
      .poll(
        async () => {
          const state = await request<Run>(
            page,
            `/api/v1/analytics/runs/${run.id}`,
          );
          return state.worker_last_seen
            ? Date.now() - Date.parse(state.worker_last_seen)
            : 0;
        },
        { timeout: 75000, intervals: [1000] },
      )
      .toBeGreaterThan(60000);
    offline = await request<Run>(page, `/api/v1/analytics/runs/${run.id}`);
    expect(offline.status).toBe("PASS");
    expect(offline.pipeline_status).toBe("OUTAGE");
    expect(offline.result_sha256).toBe(before.result_sha256);
    expect(offline.last_successful_sync).toBe(before.last_successful_sync);
    expect(offline.headcount).toBe(5000);
    await expect(selected.getByRole("status")).toHaveText("PASS · OUTAGE", {
      timeout: 15000,
    });
    await expect(selected.getByRole("alert")).toContainText(
      "analytical pipeline is unavailable",
    );
    await expect(executive.getByRole("status")).toHaveText("OUTAGE", {
      timeout: 15000,
    });
    expect(
      (
        await request<components["schemas"]["AnalyticsPublic"]>(
          executive,
          "/api/v1/analytics/public",
        )
      ).pipeline_status,
    ).toBe("OUTAGE");
    expect(
      await (
        await page.request.get(`/api/v1/analytics/runs/${run.id}/manifest`)
      ).body(),
    ).toEqual(originalManifest);
    await page.screenshot({
      path: `docs/evidence/p11-${info.project.name}-completed-outage.png`,
      fullPage: true,
    });
  } finally {
    if (resume) await resume();
    await executive.close();
  }
  await expect(selected.getByRole("status")).toHaveText("PASS · CURRENT", {
    timeout: 20000,
  });
  const restored = await request<Run>(page, `/api/v1/analytics/runs/${run.id}`);
  expect(restored.result_sha256).toBe(before.result_sha256);
  expect(restored.last_successful_sync).toBe(before.last_successful_sync);
  expect(restored.comparisons).toEqual(before.comparisons);
  expect(
    await (
      await page.request.get(`/api/v1/analytics/runs/${run.id}/manifest`)
    ).body(),
  ).toEqual(originalManifest);
  await writeFile(
    `docs/evidence/p11-${info.project.name}-completed-outage.json`,
    JSON.stringify(
      {
        claim_scope: containerRuntime
          ? "actual Compose synthetic OIDC/API/browser/worker heartbeat outage"
          : "actual native synthetic OIDC/API/browser/worker heartbeat outage",
        run_id: run.id,
        expected_headcount: 5000,
        before_pipeline_status: before.pipeline_status,
        offline_pipeline_status: offline!.pipeline_status,
        restored_pipeline_status: restored.pipeline_status,
        result_sha256_before: before.result_sha256,
        result_sha256_after: restored.result_sha256,
        last_successful_sync_before: before.last_successful_sync,
        last_successful_sync_after: restored.last_successful_sync,
        observed_heartbeat_boundary_seconds: 60,
        manifest_bytes_unchanged: true,
        no_page_reload_or_manual_refresh: true,
        cloud_validation: "UNVERIFIED",
      },
      null,
      2,
    ) + "\n",
  );
});
