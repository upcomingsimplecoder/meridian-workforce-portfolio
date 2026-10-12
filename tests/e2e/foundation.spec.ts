import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setTimeout as idle } from "node:timers/promises";
import { signIn } from "./helpers";

for (const persona of [
  "hr",
  "finance",
  "manager-a",
  "manager-b",
  "approver",
  "executive",
  "auditor",
  "steward",
]) {
  test(`real OIDC login persists ${persona} and logout revokes session`, async ({
    page,
    context,
  }) => {
    await signIn(page, persona);
    const me = await (await page.request.get("/api/v1/me")).json();
    expect(me.id).toBe(persona);
    expect(
      (await context.cookies()).find((c) => c.name === "meridian_session")
        ?.httpOnly,
    ).toBe(true);
    const overview = await (await page.request.get("/api/v1/overview")).json();
    expect(overview.demo_accounts).toBe(8);
    await page.reload();
    await expect(
      page.getByRole("heading", { name: /Workspace overview/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(
      page.getByRole("link", { name: /Sign in to Meridian/ }),
    ).toBeVisible();
    expect((await page.request.get("/api/v1/me")).status()).toBe(401);
  });
}

test("invalid credentials never create an authenticated session", async ({
  page,
}) => {
  await page.goto("/auth/login");
  await page.getByLabel("Username").fill("hr");
  await page.getByLabel("Password").fill("incorrect-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "Incorrect username or password.",
  );
  expect(
    (await page.request.get("http://127.0.0.1:3017/api/v1/me")).status(),
  ).toBe(401);
});

test("a stale page signs out cleanly after its real session is revoked", async ({
  page,
}) => {
  await signIn(page, "finance");
  const me = await (await page.request.get("/api/v1/me")).json();
  const revoked = await page.request.post("/api/v1/logout", {
    headers: { Origin: "http://127.0.0.1:3017", "X-CSRF-Token": me.csrf_token },
  });
  expect(revoked.status()).toBe(204);
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("link", { name: /Sign in to Meridian/ }),
  ).toBeVisible();
  await expect(
    page.getByText("Sign out failed. Please try again."),
  ).toHaveCount(0);
  expect((await page.request.get("/api/v1/me")).status()).toBe(401);
});

test("authenticated reads survive the upstream idle connection boundary", async ({
  page,
}) => {
  await signIn(page, "hr");
  for (const pause of [4950, 5050]) {
    const before = await page.request.get("/api/v1/me");
    expect(before.status()).toBe(200);
    expect((await before.json()).id).toBe("hr");
    expect(before.headers()["connection"]).toBe("close");
    // Exercise either side of Uvicorn's five-second idle timeout. This is a
    // connection-lifecycle check, not a wait for application readiness.
    await idle(pause);
    const after = await page.request.get("/api/v1/me");
    expect(after.status()).toBe(200);
    expect((await after.json()).id).toBe("hr");
  }
  await page.getByRole("link", { name: "Talent", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "New talent cycle", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Connection interrupted.")).toHaveCount(0);
});

test("foundation navigation, keyboard, mobile and accessibility", async ({
  page,
}, testInfo) => {
  await signIn(page, "hr");
  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
  await page.screenshot({
    path: `docs/evidence/${process.env.EVIDENCE_PHASE || "current"}-${testInfo.project.name}-overview.png`,
    fullPage: true,
  });
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await page.getByRole("link", { name: "Scenario Lab", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Scenario Lab",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Your plans and assigned reviews" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "New plan", exact: true }),
  ).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Your access" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const mobile = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(mobile.violations).toEqual([]);
  await page.screenshot({
    path: `docs/evidence/${process.env.EVIDENCE_PHASE || "current"}-${testInfo.project.name}-mobile.png`,
    fullPage: true,
  });
});
