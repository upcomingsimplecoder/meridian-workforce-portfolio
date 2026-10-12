import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
const credentials: Record<string, string> = JSON.parse(
  readFileSync(
    process.env.MERIDIAN_E2E_CREDENTIALS || ".local/demo-credentials.json",
    "utf8",
  ),
);
export async function signIn(page: Page, persona: string) {
  // The full three-browser suite shares one loopback IP. Respect the real IdP's
  // 30/minute budget instead of relaxing the application's authentication control.
  test.setTimeout(Math.max(test.info().timeout, 90000));
  for (let attempt = 0; attempt < 2; attempt++) {
    await page.goto("/");
    await page.getByRole("link", { name: /Sign in to Meridian/ }).click();
    await page.getByLabel("Username").fill(persona);
    await page.getByLabel("Password").fill(credentials[persona]);
    const [response] = await Promise.all([
      page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname.endsWith("/login") &&
          response.request().method() === "POST",
      ),
      page.getByRole("button", { name: "Sign in", exact: true }).click(),
    ]);
    if (response.status() === 429 && attempt === 0) {
      const seconds = Number(response.headers()["retry-after"]);
      expect(seconds).toBeGreaterThan(0);
      expect(seconds).toBeLessThanOrEqual(60);
      await test.info().attach("identity-rate-limit", {
        body: JSON.stringify({ status: 429, retry_after_seconds: seconds }),
        contentType: "application/json",
      });
      // This wait follows the server's Retry-After contract, not a readiness guess.
      await page.waitForTimeout(seconds * 1000);
      continue;
    }
    await expect(
      page.getByRole("heading", { name: /Workspace overview/ }),
    ).toBeVisible();
    return;
  }
  throw new Error(
    "Identity rate limit persisted after the advertised Retry-After period",
  );
}

export async function workforceName(page: Page, id: string) {
  const snapshot = await page.getByLabel("Source snapshot").inputValue();
  const effective = await page.getByLabel("Effective date").inputValue();
  const response = await page.request.get(`/api/v1/people/${id}`, {
    params: { snapshot_id: snapshot, as_of: effective },
  });
  expect(response.status()).toBe(200);
  const person = await response.json();
  expect(person.id).toBe(id);
  expect(person.display_name.length).toBeGreaterThan(3);
  return `${person.display_name} ${id}`;
}

export async function chooseHistoricalSource(
  page: Page,
  label: string,
  title = "June workforce · corrected",
  query = "June workforce",
) {
  await page
    .getByLabel("Search published sources", { exact: true })
    .fill(query);
  await page.getByRole("button", { name: "Find source", exact: true }).click();
  await expect(
    page.getByLabel(label, { exact: true }).locator("option", {
      hasText: title,
    }),
  ).toHaveCount(1);
  await page.getByLabel(label, { exact: true }).selectOption({ label: title });
}
