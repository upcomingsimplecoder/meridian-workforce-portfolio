import { chromium, expect } from "@playwright/test";
import {
  readFileSync,
  writeFileSync,
  existsSync,
  mkdirSync,
  chmodSync,
} from "node:fs";
import { resolve, join } from "node:path";
const directory = resolve(process.argv[2]);
if (
  !directory.startsWith(resolve(".local") + "/") ||
  !directory.includes("db-reconnect-")
)
  throw new Error("Owned private drill directory required");
const credentials = JSON.parse(
  readFileSync(".local/demo-credentials.json", "utf8"),
);
const browser = await chromium.launch();
const context = await browser.newContext({
  baseURL: "http://127.0.0.1:3017",
  viewport: { width: 1440, height: 1050 },
});
await context.tracing.start({
  screenshots: true,
  snapshots: true,
  sources: true,
});
const page = await context.newPage();
async function wait(name) {
  const end = Date.now() + 45000;
  while (!existsSync(join(directory, name)) && Date.now() < end)
    await new Promise((r) => setTimeout(r, 20));
  if (!existsSync(join(directory, name)))
    throw new Error("Owned drill barrier timed out");
}
try {
  await page.goto("http://127.0.0.1:3017/auth/login");
  await page.getByLabel("Username").fill("hr");
  await page.getByLabel("Password").fill(credentials.hr);
  await page.getByRole("button", { name: "Sign in securely" }).click();
  await expect(
    page.getByRole("heading", { name: /Workspace overview/ }),
  ).toBeVisible({ timeout: 30000 });
  writeFileSync(join(directory, "browser-ready"), "ready", { mode: 0o600 });
  await wait("database-stopped");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Connection interrupted." }),
  ).toBeVisible({ timeout: 30000 });
  const ready = (
    await page.request.get("http://127.0.0.1:8017/ready")
  ).status();
  const health = (
    await page.request.get("http://127.0.0.1:8017/health")
  ).status();
  expect(ready).toBe(503);
  expect(health).toBe(200);
  await page.screenshot({
    path: "docs/evidence/p10-db-reconnect-outage.png",
    fullPage: true,
  });
  writeFileSync(
    join(directory, "outage-observed.json"),
    JSON.stringify({ ready, health, visible_error: "Connection interrupted." }),
    { mode: 0o600 },
  );
  await wait("database-started");
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(
    page.getByRole("heading", { name: /Workspace overview/ }),
  ).toBeVisible({ timeout: 30000 });
  expect((await page.request.get("http://127.0.0.1:8017/ready")).status()).toBe(
    200,
  );
  expect((await page.request.get("/api/v1/me")).status()).toBe(200);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("link", { name: /Sign in to Meridian/ }).click();
  await page.getByLabel("Username").fill("hr");
  await page.getByLabel("Password").fill(credentials.hr);
  await page.getByRole("button", { name: "Sign in securely" }).click();
  await expect(
    page.getByRole("heading", { name: /Workspace overview/ }),
  ).toBeVisible({ timeout: 30000 });
  await page.screenshot({
    path: "docs/evidence/p10-db-reconnect-recovered.png",
    fullPage: true,
  });
  writeFileSync(
    join(directory, "recovered.json"),
    JSON.stringify({
      ready: 200,
      same_session: 200,
      fresh_oidc_login: 200,
      visible_recovery: "Workspace overview",
    }),
    { mode: 0o600 },
  );
} finally {
  mkdirSync(".local/evidence/p10-db-reconnect", {
    recursive: true,
    mode: 0o700,
  });
  await context.tracing.stop({
    path: ".local/evidence/p10-db-reconnect/trace.zip",
  });
  chmodSync(".local/evidence/p10-db-reconnect/trace.zip", 0o600);
  await browser.close();
}
