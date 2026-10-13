import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { pauseWorker } from "./runtime-control";
import { randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";
import { signIn, chooseHistoricalSource } from "./helpers";
import type { components } from "../../packages/contracts/api";
type Batch = components["schemas"]["BatchView"];
const fixturePeople = JSON.parse(
  readFileSync("fixtures/workforce/corrected.json", "utf8"),
).records as { employee_id: string; display_name: string }[];
function previewName(id: string) {
  const person = fixturePeople.find((row) => row.employee_id === id);
  expect(person).toBeDefined();
  return `${person!.display_name} ${id} · ACTIVE`;
}

// Each run contributes real immutable source evidence. A unique synthetic job-family
// label changes the input hash while preserving all numerical expectations and the 40 defects.
function uniqueFamily() {
  return `Research QA ${randomUUID()
    .replaceAll("-", "")
    .replace(/[0-9]/g, (digit) => String.fromCharCode(103 + Number(digit)))}`;
}
async function csvFixture(defects: boolean, family: string) {
  const source = await readFile(
    `fixtures/workforce/${defects ? "quarantine-40" : "corrected"}.csv`,
    "utf8",
  );
  return Buffer.from(source.replace(",Research,", `,${family},`));
}
async function importFromForm(
  page: Page,
  title: string,
  bytes: Buffer,
  correction = false,
) {
  if (!correction) {
    await page.getByRole("button", { name: "Import workforce" }).click();
    await chooseHistoricalSource(page, "Base snapshot");
  }
  await page.getByLabel("Source title", { exact: true }).fill(title);
  await page.getByLabel("Source watermark").fill("2027-07-31");
  await page
    .getByLabel("Workforce source file", { exact: true })
    .setInputFiles({
      name: "synthetic-workforce.csv",
      mimeType: "text/csv",
      buffer: bytes,
    });
  const [registered] = await Promise.all([
    page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/ingestion/batches" &&
        response.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Upload and validate" }).click(),
  ]);
  expect(registered.status()).toBe(201);
  return (await registered.json()) as Batch;
}
async function noAxeViolations(page: Page) {
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
}

test("steward quarantines 40 defects, corrects 5000 records and publishes a sealed source", async ({
  page,
}, info) => {
  await signIn(page, "steward");
  await page.getByRole("link", { name: "Data Quality", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Data Quality." }),
  ).toBeVisible();
  const family = uniqueFamily();
  const title = `${info.project.name} July ${randomUUID().slice(0, 8)}`;
  const input = await csvFixture(true, family);
  const begin = performance.now();
  const quarantined = await importFromForm(page, title, input);
  await expect(page.getByTestId("batch-state")).toHaveText("QUARANTINED", {
    timeout: 60000,
  });
  const validationMs = performance.now() - begin;
  expect(validationMs).toBeLessThan(60000);
  await expect(page.getByTestId("input-count")).toHaveText("5,000");
  await expect(page.getByTestId("accepted-count")).toHaveText("4,960");
  await expect(page.getByTestId("rejected-count")).toHaveText("40");
  await expect(
    page.getByRole("button", { name: "Review publication" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("40 matching validation errors", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Error code").selectOption("UNKNOWN_ORG");
  await expect(
    page.getByText("10 matching validation errors", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".batch-choice[aria-pressed=true]")).toContainText(
    "QUARANTINED",
  );
  await noAxeViolations(page);
  await page.screenshot({
    path: `docs/evidence/p03-${info.project.name}-quarantine.png`,
    fullPage: true,
  });
  const [errorsDownload] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download redacted errors" }).click(),
  ]);
  const errors = await readFile((await errorsDownload.path())!, "utf8");
  expect(errors.trim().split("\n")).toHaveLength(41);
  expect(errors).not.toMatch(/annual_pay|SYN-999999|XXX|D99/);
  await page
    .getByRole("button", { name: "Accepted preview", exact: true })
    .click();
  await expect(
    page.getByRole("cell", {
      name: previewName("SYN-000001"),
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next rows", exact: true }).click();
  await expect(
    page.getByRole("cell", {
      name: previewName("SYN-000021"),
    }),
  ).toBeVisible();
  expect(await page.locator("main").innerText()).not.toContain(
    "annual_pay_minor",
  );
  await page
    .getByRole("button", { name: "Upload correction", exact: true })
    .click();
  const corrected = await importFromForm(
    page,
    `${title} corrected`,
    await csvFixture(false, family),
    true,
  );
  expect(corrected.supersedes_batch_id).toBe(quarantined.id);
  await expect(page.getByTestId("batch-state")).toHaveText("VALIDATED", {
    timeout: 60000,
  });
  await expect(page.getByTestId("accepted-count")).toHaveText("5,000");
  await expect(page.getByTestId("rejected-count")).toHaveText("0");
  await page.getByRole("button", { name: "Review publication" }).click();
  const modal = page.getByRole("dialog", {
    name: "Confirm source publication",
  });
  await expect(
    modal.getByRole("button", { name: "Publish immutable snapshot" }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(modal).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Review publication" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await modal.getByRole("checkbox").check();
  await noAxeViolations(page);
  await page.screenshot({
    path: `docs/evidence/p03-${info.project.name}-confirmation.png`,
    fullPage: false,
  });
  const publishStart = performance.now();
  const [publishedResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(`/${corrected.id}/publish`) &&
        response.request().method() === "POST",
    ),
    modal.getByRole("button", { name: "Publish immutable snapshot" }).click(),
  ]);
  expect(publishedResponse.status()).toBe(200);
  const published = (await publishedResponse.json()) as Batch;
  await expect(page.getByTestId("batch-state")).toHaveText("PUBLISHED");
  await expect(
    page.getByRole("button", { name: "Download manifest", exact: true }),
  ).toBeEnabled({ timeout: 15000 });
  const publishAndManifestMs = performance.now() - publishStart;
  const [manifestDownload] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", { name: "Download manifest", exact: true })
      .click(),
  ]);
  const manifest = JSON.parse(
    await readFile((await manifestDownload.path())!, "utf8"),
  );
  expect(manifest).toMatchObject({
    snapshot_id: published.snapshot_id,
    accepted: 5000,
    rejected: 0,
    total: 5000,
    person_identities: 5000,
    employee_versions: 10002,
    supersedes_batch_id: quarantined.id,
  });
  await page
    .getByText("Source lineage and immutable evidence", { exact: true })
    .click();
  await expect(
    page.getByText(published.snapshot_id!, { exact: true }),
  ).toBeVisible();
  await noAxeViolations(page);
  await page.screenshot({
    path: `docs/evidence/p03-${info.project.name}-published.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await noAxeViolations(page);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: `docs/evidence/p03-${info.project.name}-mobile.png`,
    fullPage: true,
  });
  await page.getByRole("link", { name: "Explore snapshot" }).click();
  await expect(page.getByLabel("Source snapshot")).toHaveValue(
    published.snapshot_id!,
  );
  await expect(page.getByLabel("Effective date")).toHaveValue("2027-07-31");
  await expect(page.getByTestId("headcount")).toHaveText("5,000");
  await expect(page.getByTestId("fte")).toHaveText("4900.0000");
  const june = await (
    await page.request.get(
      `/api/v1/people/SYN-000037?snapshot_id=${published.snapshot_id}&as_of=2027-06-30`,
    )
  ).json();
  const july = await (
    await page.request.get(
      `/api/v1/people/SYN-000037?snapshot_id=${published.snapshot_id}&as_of=2027-07-31`,
    )
  ).json();
  expect(june.org_id).toBe("D02");
  expect(july.org_id).toBe("D01");
  expect(june.source_batch_id).not.toBe(corrected.id);
  expect(july.source_batch_id).toBe(corrected.id);
  const observed = {
    browser: info.project.name,
    dataset: "5000 synthetic identities; 40 intentional rejects",
    upload_bytes: input.byteLength,
    validation_ms: Math.round(validationMs),
    publication_and_manifest_ms: Math.round(publishAndManifestMs),
    batch_id: corrected.id,
    snapshot_id: published.snapshot_id,
    expected: {
      accepted: 5000,
      rejected: 0,
      headcount: 5000,
      fte: "4900.0000",
      employee_versions: 10002,
    },
  };
  await writeFile(
    `docs/evidence/p03-${info.project.name}-journey.json`,
    JSON.stringify(observed, null, 2) + "\n",
  );
  await info.attach("persisted-ingestion-result", {
    body: JSON.stringify(observed),
    contentType: "application/json",
  });
});

test("concurrent publication has one winner and same-key replay returns its snapshot", async ({
  page,
}, info) => {
  await signIn(page, "hr");
  const me = await (await page.request.get("/api/v1/me")).json();
  const snapshots = await (
    await page.request.get("/api/v1/snapshots?source_search=June%20workforce")
  ).json();
  const base = snapshots.find(
    (item: { title: string }) => item.title === "June workforce · corrected",
  );
  const document = JSON.parse(
    await readFile("fixtures/workforce/corrected.json", "utf8"),
  );
  document.records = [document.records[0]];
  document.records[0].job_family = uniqueFamily();
  const bytes = Buffer.from(JSON.stringify(document));
  const sha = await import("node:crypto").then((crypto) =>
    crypto.createHash("sha256").update(bytes).digest("hex"),
  );
  const headers = {
    Origin: "http://127.0.0.1:3017",
    "X-CSRF-Token": me.csrf_token,
    "Idempotency-Key": randomUUID(),
  };
  const intentResponse = await page.request.post("/api/v1/ingestion/uploads", {
    headers,
    data: {
      media_type: "application/json",
      sha256: sha,
      size_bytes: bytes.length,
    },
  });
  expect(intentResponse.status()).toBe(201);
  const intent = await intentResponse.json();
  expect(
    (
      await page.request.put(intent.upload_url, {
        headers: { ...headers, "Content-Type": "application/json" },
        data: bytes,
      })
    ).status(),
  ).toBe(200);
  const response = await page.request.post("/api/v1/ingestion/batches", {
    headers: { ...headers, "Idempotency-Key": randomUUID() },
    data: {
      upload_id: intent.id,
      base_snapshot_id: base.id,
      title: `Concurrent ${info.project.name}`,
      watermark: "2027-07-31",
      schema_version: 1,
    },
  });
  expect(response.status()).toBe(201);
  const batch = await response.json();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`/api/v1/ingestion/batches/${batch.id}`)
          ).json()
        ).state,
    )
    .toBe("VALIDATED");
  const ready = await (
    await page.request.get(`/api/v1/ingestion/batches/${batch.id}`)
  ).json();
  const keys = [randomUUID(), randomUUID()];
  const target = `/api/v1/ingestion/batches/${batch.id}/publish`;
  const publish = (key: string) =>
    page.request.post(target, {
      headers: {
        ...headers,
        "Idempotency-Key": key,
        "If-Match": String(ready.version),
      },
      data: { confirm_zero_rejects: true },
    });
  const attempts = await Promise.all(keys.map(publish));
  expect(attempts.map((item) => item.status()).sort()).toEqual([200, 409]);
  const winner = attempts.findIndex((item) => item.status() === 200);
  const published = await attempts[winner].json();
  const replay = await publish(keys[winner]);
  expect(replay.status()).toBe(200);
  expect((await replay.json()).snapshot_id).toBe(published.snapshot_id);
  const allSnapshots = await (
    await page.request.get("/api/v1/snapshots")
  ).json();
  expect(
    allSnapshots.filter(
      (item: { source_batch_id: string }) => item.source_batch_id === batch.id,
    ),
  ).toHaveLength(1);
  await info.attach("concurrent-publication", {
    body: JSON.stringify({
      statuses: attempts.map((item) => item.status()),
      snapshot_id: published.snapshot_id,
      replay_status: replay.status(),
    }),
    contentType: "application/json",
  });
});

test("manager cannot load source records, previews or source exports", async ({
  page,
}) => {
  await signIn(page, "manager-a");
  // The steward-only link is hidden for managers, so exercise the denial by
  // navigating directly to the protected route.
  await page.goto("/data-quality");
  await expect(
    page.getByRole("heading", { name: "Source stewardship access required" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Import workforce" }),
  ).toHaveCount(0);
  for (const path of [
    "/api/v1/ingestion/batches",
    `/api/v1/ingestion/batches/${randomUUID()}/preview`,
    `/api/v1/ingestion/batches/${randomUUID()}/errors`,
    `/api/v1/ingestion/batches/${randomUUID()}/downloads/errors?token=forged`,
  ]) {
    const result = await page.request.get(path);
    expect(result.status()).toBe(403);
    expect(await result.text()).not.toMatch(
      /annual_pay|Synthetic Worker|Aisha Adler|display_name|source_sha256/,
    );
  }
  await noAxeViolations(page);
});

test("an offline worker leaves a durable visible queue and resumes validation", async ({
  page,
}, info) => {
  await signIn(page, "steward");
  await page.getByRole("link", { name: "Data Quality", exact: true }).click();
  const fixture =
    (await csvFixture(false, uniqueFamily()))
      .toString("utf8")
      .split("\n")
      .slice(0, 2)
      .join("\n") + "\n";
  let registered: Batch | undefined;
  const resume = await pauseWorker("outbox");
  try {
    registered = await importFromForm(
      page,
      `Worker outage ${info.project.name}`,
      Buffer.from(fixture),
    );
    await expect(page.getByTestId("batch-state")).toHaveText("RECEIVED");
    await expect(
      page.getByText(
        /If the worker is offline, the durable job remains queued/,
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Review publication" }),
    ).toHaveCount(0);
    await page.reload();
    await page
      .getByRole("button")
      .filter({ hasText: `Worker outage ${info.project.name}` })
      .first()
      .click();
    await expect(page.getByTestId("batch-state")).toHaveText("RECEIVED");
    const stored = await (
      await page.request.get(`/api/v1/ingestion/batches/${registered.id}`)
    ).json();
    expect(stored).toMatchObject({
      state: "RECEIVED",
      pending_job: true,
      total_count: null,
      snapshot_id: null,
    });
    await page.screenshot({
      path: `docs/evidence/p03-${info.project.name}-worker-queue.png`,
      fullPage: false,
    });
  } finally {
    await resume();
  }
  await expect(page.getByTestId("batch-state")).toHaveText("VALIDATED", {
    timeout: 15000,
  });
  await expect(page.getByTestId("accepted-count")).toHaveText("1");
  const stored = await (
    await page.request.get(`/api/v1/ingestion/batches/${registered!.id}`)
  ).json();
  expect(stored).toMatchObject({
    state: "VALIDATED",
    pending_job: false,
    total_count: 1,
    accepted_count: 1,
    snapshot_id: null,
  });
  await info.attach("worker-outage-recovery", {
    body: JSON.stringify({
      batch_id: stored.id,
      state: stored.state,
      accepted: stored.accepted_count,
    }),
    contentType: "application/json",
  });
});
