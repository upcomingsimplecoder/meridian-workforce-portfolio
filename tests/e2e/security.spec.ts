import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { request as httpRequest } from "node:http";
import { writeFile } from "node:fs/promises";
import { signIn } from "./helpers";
import { measurementEnvironment } from "./environment";

test.use({ bypassCSP: false });

test("fresh document nonces block injected scripts while real authenticated navigation works", async ({
  page,
}, info) => {
  await page.route("**/", async (route) => {
    if (route.request().resourceType() !== "document") return route.continue();
    const original = await route.fetch({
      headers: { ...route.request().headers(), "accept-encoding": "identity" },
    });
    const html = (await original.text()).replace(
      "<head>",
      "<head><script data-security-probe>window.__meridianInjected = true</script>",
    );
    await route.fulfill({ response: original, body: html });
  });
  const document = await page.goto("/");
  expect(document).not.toBeNull();
  const csp = document!.headers()["content-security-policy"];
  const nonce = /'nonce-([^']+)'/.exec(csp)![1];
  const scriptPolicy = csp
    .split(";")
    .find((s) => s.trim().startsWith("script-src"))!;
  expect(scriptPolicy).toContain("'strict-dynamic'");
  expect(scriptPolicy).not.toContain("'unsafe-inline'");
  expect(scriptPolicy).not.toContain("'unsafe-eval'");
  const separate = await page.request.get("/", {
    headers: {
      "x-nonce": "forged",
      "Content-Security-Policy": "script-src 'unsafe-inline'",
    },
  });
  expect(separate.status()).toBe(200);
  expect(separate.headers()["content-security-policy"]).not.toContain(nonce);
  expect(separate.headers()["content-security-policy"]).not.toContain("forged");
  expect(separate.headers()["cache-control"]).toContain("no-store");
  const inline = await page
    .locator("script:not([src]):not([data-security-probe])")
    .evaluateAll((nodes) =>
      nodes
        .filter(
          (node) =>
            !node.getAttribute("type") ||
            node.getAttribute("type") === "text/javascript",
        )
        .map((node) => (node as HTMLScriptElement).nonce),
    );
  expect(inline.length).toBeGreaterThan(0);
  expect(inline.every((value) => value === nonce)).toBe(true);
  await expect(page.locator("script[data-security-probe]")).toHaveCount(1);
  expect(await page.evaluate(() => "__meridianInjected" in window)).toBe(false);
  await page.unroute("**/");
  await signIn(page, "hr");
  await page.getByRole("link", { name: "Workforce", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /Workforce Explorer/ }),
  ).toBeVisible();
  const response = await page.request.get("/api/v1/me");
  expect(response.status()).toBe(200);
  expect((await response.json()).id).toBe("hr");
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: "docs/evidence/p10-security-" + info.project.name + ".png",
  });
});

test("real chunked oversized requests reject before logout and recovery preserves the session", async ({
  page,
  context,
}, info) => {
  await signIn(page, "finance");
  const me = await (await page.request.get("/api/v1/me")).json();
  const cookie = (await context.cookies()).find(
    (row) => row.name === "meridian_session",
  )!;
  const result = await new Promise<{
    status: number;
    headers: Record<string, unknown>;
    body: string;
  }>((resolve, reject) => {
    const request = httpRequest(
      {
        hostname: "127.0.0.1",
        port: 3017,
        method: "POST",
        path: "/api/v1/logout",
        headers: {
          "Transfer-Encoding": "chunked",
          "Content-Type": "application/json",
          Origin: "http://127.0.0.1:3017",
          "X-CSRF-Token": me.csrf_token,
          Cookie: cookie.name + "=" + cookie.value,
        },
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () =>
          resolve({
            status: response.statusCode!,
            headers: response.headers,
            body,
          }),
        );
      },
    );
    request.on("error", reject);
    for (let index = 0; index < 9; index++)
      request.write(Buffer.alloc(1024 * 1024, "x"));
    request.end();
  });
  expect(result.status).toBe(413);
  expect(result.headers["cache-control"]).toBe("no-store");
  const problem = JSON.parse(result.body);
  expect(problem.request_id).toBe(result.headers["x-request-id"]);
  expect(problem.title).toBe("Payload Too Large");
  expect((await (await page.request.get("/api/v1/me")).json()).id).toBe(
    "finance",
  );
  const forged = await page.request.get("/api/v1/me", {
    headers: { Host: "attacker.example" },
  });
  expect(forged.status()).toBe(400);
  const overlong = await page.request.get("/api/v1/me?" + "q".repeat(9000));
  expect(overlong.status()).toBe(414);
  await writeFile(
    "docs/evidence/p10-security-" + info.project.name + "-journey.json",
    JSON.stringify(
      {
        scope:
          "actual browser/OIDC/Next proxy/API/PostgreSQL; execution recorded below; public HTTPS deployment UNVERIFIED",
        execution_context: measurementEnvironment(),
        streamed_request_bytes: 9 * 1024 * 1024,
        oversized_status: result.status,
        session_after_rejection: "finance",
        untrusted_host_status: forged.status(),
        overlong_query_status: overlong.status(),
        request_id: problem.request_id,
      },
      null,
      2,
    ) + "\n",
  );
});
