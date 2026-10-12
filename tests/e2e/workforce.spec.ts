import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile } from "node:fs/promises";
import { signIn, workforceName, chooseHistoricalSource } from "./helpers";
async function explorer(page: Page, persona: string) {
  await signIn(page, persona);
  await page.getByRole("link", { name: "Workforce", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Workforce Explorer." }),
  ).toBeVisible();
  // Pin the historical fixture while imports publish newer snapshots.
  await chooseHistoricalSource(page, "Source snapshot");
  await expect(page.getByTestId("headcount")).toBeVisible();
}
async function search(page: Page, id: string) {
  await page.getByLabel("Search people", { exact: true }).fill(id);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByText("1 matching people", { exact: true }),
  ).toBeVisible();
}

test("HR explores real pages, corrected history, source freeze and scoped CSV", async ({
  page,
}, info) => {
  await explorer(page, "hr");
  await expect(page.getByTestId("headcount")).toHaveText("5,000");
  await expect(page.getByTestId("fte")).toHaveText("4900.0000");
  await expect(
    page.getByRole("button", { name: await workforceName(page, "SYN-000001") }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(
    page.getByRole("button", { name: await workforceName(page, "SYN-000026") }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: await workforceName(page, "SYN-000001") }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(
    page.getByRole("columnheader", { name: "Annual base pay" }),
  ).toBeVisible();
  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: `docs/evidence/p02-${info.project.name}-workforce.png`,
    fullPage: false,
  });
  await search(page, "SYN-000037");
  const trigger = page.getByRole("button", {
    name: await workforceName(page, "SYN-000037"),
  });
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("2027-03-01 → Open interval")).toBeVisible();
  await expect(dialog.getByText("2027-01-01 → 2027-03-01")).toBeVisible();
  await dialog.getByText("Source evidence", { exact: true }).last().click();
  await expect(
    dialog.getByText("Recorded", { exact: true }).last(),
  ).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: `docs/evidence/p02-${info.project.name}-history.png`,
    fullPage: false,
  });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export matching" }).click(),
  ]);
  const file = await download.path();
  expect(file).not.toBeNull();
  const csv = await readFile(file!, "utf8");
  expect(csv.trim().split("\n")).toHaveLength(2);
  expect(csv).toContain(
    `SYN-000037,${(await workforceName(page, "SYN-000037")).replace(" SYN-000037", "")},D02`,
  );
  await page.getByLabel("Effective date").fill("2027-02-28");
  await search(page, "SYN-000037");
  await expect(
    page.getByRole("row").filter({ hasText: "SYN-000037" }),
  ).toContainText("Research & Insights");
  await chooseHistoricalSource(
    page,
    "Source snapshot",
    "February source freeze",
    "February",
  );
  await page.getByLabel("Effective date").fill("2027-06-30");
  await search(page, "SYN-000037");
  await expect(
    page.getByRole("row").filter({ hasText: "SYN-000037" }),
  ).toContainText("Research & Insights");
});

test("manager scope changes with history and blocks foreign objects and exports", async ({
  page,
}, info) => {
  await explorer(page, "manager-a");
  await expect(page.getByTestId("headcount")).toHaveText("207");
  await expect(
    page.getByText("Your reporting descendants at the selected effective date"),
  ).toBeVisible();
  for (const path of [
    "/api/v1/people/SYN-000014",
    "/api/v1/people/SYN-000014/history",
    "/api/v1/people/SYN-000049",
  ]) {
    const response = await page.request.get(path);
    expect(response.status()).toBe(404);
    expect(await response.text()).not.toContain("annual_pay");
  }
  expect(
    (await (await page.request.get("/api/v1/people?org=D02")).json()).items,
  ).toEqual([]);
  expect(
    (await (await page.request.get("/api/v1/people/export?org=D02")).text())
      .trim()
      .split("\n"),
  ).toHaveLength(1);
  expect(
    (await page.request.get("/api/v1/people?role=hr&scope=*")).status(),
  ).toBe(422);
  await page.getByLabel("Effective date").fill("2027-02-28");
  await expect(page.getByTestId("headcount")).toHaveText("416");
  await search(page, "SYN-000049");
  await page
    .getByRole("button", { name: await workforceName(page, "SYN-000049") })
    .click();
  await expect(
    page.getByRole("dialog").getByText("SYN-000013", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: `docs/evidence/p02-${info.project.name}-manager.png`,
    fullPage: false,
  });
});

test("executive sees only governed aggregates in both UI and network", async ({
  page,
}, info) => {
  const bodies: string[] = [];
  page.on("response", async (response) => {
    if (response.url().includes("/api/v1/") && response.ok())
      bodies.push(await response.text());
  });
  await explorer(page, "executive");
  await expect(
    page.getByRole("heading", { name: "Department distribution" }),
  ).toBeVisible();
  await expect(page.getByTestId("headcount")).toHaveText("5,000");
  await expect(
    page.getByRole("button", { name: "Export matching" }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Search people", { exact: true })).toHaveCount(
    0,
  );
  for (const path of [
    "/api/v1/people",
    "/api/v1/people/SYN-000001/history",
    "/api/v1/people/export",
  ])
    expect((await page.request.get(path)).status()).toBe(403);
  expect(bodies.length).toBeGreaterThan(2);
  expect(bodies.join("\n")).not.toMatch(
    /annual_pay|minor_units|Synthetic Worker|Aisha Adler|SYN-[0-9]{6}|row_count/,
  );
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: `docs/evidence/p02-${info.project.name}-aggregates.png`,
    fullPage: false,
  });
});

test("steward fields stay restricted and mobile search, filters and dialog are accessible", async ({
  page,
}, info) => {
  await explorer(page, "steward");
  await expect(
    page.getByRole("columnheader", { name: "Annual base pay" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Export matching" }),
  ).toHaveCount(0);
  const raw = await (await page.request.get("/api/v1/people")).text();
  expect(raw).not.toMatch(/annual_pay|currency|minor_units/);
  expect((await page.request.get("/api/v1/people/export")).status()).toBe(403);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel("Department", { exact: true }).selectOption("D03");
  await expect(
    page.getByRole("button", { name: await workforceName(page, "SYN-000003") }),
  ).toBeVisible();
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
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: `docs/evidence/p02-${info.project.name}-mobile.png`,
    fullPage: false,
  });
  await page
    .getByRole("button", { name: await workforceName(page, "SYN-000003") })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByText(
        "Some history or fields are restricted by your current permissions.",
      ),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).not.toContainText("Annual base pay");
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.getByRole("button", { name: "Close person details" }).click();
});

test("empty and failed queries are honest and recoverable", async ({
  page,
}) => {
  await explorer(page, "hr");
  await page
    .getByLabel("Search people", { exact: true })
    .fill("does-not-exist");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "No matching people" }),
  ).toBeVisible();
  await page.route("**/api/v1/people?**", (route) => route.abort("failed"));
  await page
    .getByRole("button", { name: "Clear search and department" })
    .click();
  await expect(
    page
      .getByRole("alert")
      .getByRole("heading", { name: "Workforce is unavailable" }),
  ).toBeVisible();
  await page.unroute("**/api/v1/people?**");
  await page
    .getByRole("button", { name: "Retry workforce", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: await workforceName(page, "SYN-000001") }),
  ).toBeVisible();
});
