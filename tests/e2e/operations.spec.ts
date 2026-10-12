import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { signIn } from "./helpers";

test("real API logout event correlates to independent worker and auditor trace", async ({
  page,
}, info) => {
  await signIn(page, "hr");
  const me = await (await page.request.get("/api/v1/me")).json();
  const logout = await page.request.post("/api/v1/logout", {
    headers: {
      "X-CSRF-Token": me.csrf_token,
      Origin: "http://127.0.0.1:3017",
      traceparent: "00-" + "f".repeat(32) + "-" + "f".repeat(16) + "-01",
      baggage: "salary=DO_NOT_EXPORT_PRIVATE",
    },
  });
  expect(logout.status()).toBe(204);
  const requestId = logout.headers()["x-request-id"];
  await signIn(page, "auditor");
  let traceId = "";
  await expect
    .poll(
      async () => {
        const observed = await page.request.get("/api/v1/operations");
        expect(observed.status()).toBe(200);
        const data = await observed.json();
        const root = data.recent_spans.find(
          (s: { request_id: string; service: string }) =>
            s.request_id === requestId && s.service === "meridian-api",
        );
        traceId = root?.trace_id || "";
        return !!traceId;
      },
      { timeout: 30000 },
    )
    .toBe(true);
  expect(traceId).not.toBe("f".repeat(32));
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/v1/operations/traces/${traceId}`,
        );
        expect(response.status()).toBe(200);
        const data = await response.json();
        const root = data.spans.find(
          (s: { service: string }) => s.service === "meridian-api",
        );
        const child = data.spans.find(
          (s: { service: string }) => s.service === "meridian-worker",
        );
        if (!root || !child) return false;
        expect(root.route).toBe("/api/v1/logout");
        expect(root.http_status).toBe(204);
        expect(root.parent_span_id).toBeNull();
        expect(child.parent_span_id).toBe(root.span_id);
        expect(child.request_id).toBe(requestId);
        expect(child.event_type).toBe("identity.logout.v1");
        expect(child.outcome).toBe("OK");
        expect(JSON.stringify(data)).not.toContain("DO_NOT_EXPORT_PRIVATE");
        return true;
      },
      { timeout: 30000 },
    )
    .toBe(true);
  await page.goto("/operations");
  await expect(
    page.getByRole("heading", { name: "Recent completed spans" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: `Inspect trace ${traceId}` })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "Trace correlation" }),
  ).toBeVisible();
  await expect(
    page
      .getByLabel("Trace details")
      .getByText(/meridian-worker · CONSUMER · OK/),
  ).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: `docs/evidence/${process.env.EVIDENCE_PHASE || "current"}-${info.project.name}-operations.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("region", { name: "Span table" }).focus();
  await expect(page.getByRole("region", { name: "Span table" })).toBeFocused();
  await page.keyboard.press("ArrowRight");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 2,
    ),
  ).toBe(true);
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: `docs/evidence/${process.env.EVIDENCE_PHASE || "current"}-${info.project.name}-operations-mobile.png`,
    fullPage: true,
  });
});

for (const persona of ["executive", "manager-b", "steward"]) {
  test(`operations and direct trace reads deny ${persona}`, async ({
    page,
  }) => {
    await signIn(page, persona);
    await page.goto("/operations");
    await expect(
      page.getByRole("heading", { name: "Operational access is restricted" }),
    ).toBeVisible();
    for (const url of [
      "/api/v1/operations",
      "/api/v1/operations/traces/" + "a".repeat(32),
    ]) {
      const response = await page.request.get(url);
      expect(response.status()).toBe(403);
      expect(await response.text()).not.toContain("recent_spans");
    }
    expect(
      await page.getByRole("button", { name: /Inspect trace/ }).count(),
    ).toBe(0);
  });
}
