import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { pauseWorker } from "./runtime-control";
import { signIn, chooseHistoricalSource } from "./helpers";
const root = "/api/v1/talent";
async function studio(page: Page, persona: string, title?: string) {
  page.setDefaultTimeout(20000);
  if (
    await page.getByRole("button", { name: "Sign out", exact: true }).count()
  ) {
    const [r] = await Promise.all([
      page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === "/api/v1/logout" &&
          r.request().method() === "POST",
      ),
      page.getByRole("button", { name: "Sign out", exact: true }).click(),
    ]);
    expect(r.status()).toBe(204);
    await expect(
      page.getByRole("link", { name: /Sign in to Meridian/ }),
    ).toBeVisible();
  }
  await signIn(page, persona);
  test.setTimeout(480000);
  // Restricted personas may not receive a Talent link in the sidebar.
  await page.goto("/talent");
  await expect(
    page.getByRole("heading", { name: "Talent Review", exact: true }),
  ).toBeVisible();
  if (title) {
    await page.getByLabel("Search review cycles", { exact: true }).fill(title);
    await page.locator(".talent-cycle-link").filter({ hasText: title }).click();
    await expect(
      page.getByRole("heading", { name: title, exact: true }),
    ).toBeVisible();
  }
}
async function postClick(page: Page, route: string, label: string) {
  const [r] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/" + route) &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: label, exact: true }).click(),
  ]);
  expect(r.status()).toBe(200);
  return await r.json();
}
async function create(page: Page, title: string) {
  await page.getByRole("button", { name: "New talent cycle" }).click();
  await page.getByLabel("Review cycle title", { exact: true }).fill(title);
  await chooseHistoricalSource(page, "Workforce snapshot");
  await page
    .getByLabel("Narrative policy", { exact: true })
    .selectOption({ label: "Synthetic human narrative and calibration v1" });
  await page.getByLabel("Context effective date").fill("2028-03-31");
  await page.getByLabel("Review deadline").fill("2028-06-30");
  for (const [i, employee, author] of [
    [1, "SYN-000025", "manager-a"],
    [2, "SYN-000049", "manager-b"],
  ] as const) {
    if (i === 2)
      await page.getByRole("button", { name: "Add review assignment" }).click();
    await page
      .getByLabel("Synthetic employee " + i, { exact: true })
      .fill(employee);
    await page.getByLabel("Author " + i, { exact: true }).selectOption(author);
    await page
      .getByLabel("Calibrator " + i, { exact: true })
      .selectOption("approver");
    await page.getByLabel("HR signer " + i, { exact: true }).selectOption("hr");
  }
  await page
    .getByLabel("Configuration reason")
    .fill("Freeze synthetic context with separate human assignments");
  const [r] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === root + "/cycles" &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Save review configuration" }).click(),
  ]);
  expect(r.status()).toBe(200);
  const cycle = await r.json();
  await page
    .getByLabel("Cycle action reason")
    .fill("Launch frozen synthetic review population");
  await postClick(page, "launch", "Launch frozen review");
  await expect(page.getByRole("table")).toBeVisible();
  return cycle;
}
async function open(page: Page, employee: string) {
  const [r] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.startsWith(root + "/packets/") &&
        r.request().method() === "GET",
    ),
    page
      .getByRole("button", { name: "Open packet " + employee, exact: true })
      .click(),
  ]);
  expect(r.status()).toBe(200);
  const data = await r.json();
  expect(data.employee_id).toBe(employee);
  expect(data.display_name.length).toBeGreaterThan(3);
  await expect(
    page.getByRole("heading", {
      name: data.display_name,
      exact: true,
    }),
  ).toBeVisible();
  return data;
}
async function saveAuthor(page: Page, suffix: string) {
  await page
    .getByLabel("Accomplishments", { exact: true })
    .fill(
      "Synthetic observation: completed a documented demonstration " + suffix,
    );
  await page
    .getByLabel("Collaboration", { exact: true })
    .fill("Synthetic observation: explained the handoff to a peer " + suffix);
  await page
    .getByLabel("Development", { exact: true })
    .fill(
      "Synthetic observation: practice stronger source citations " + suffix,
    );
  if (
    !(await page.getByLabel("Synthetic observation 1", { exact: true }).count())
  )
    await page.getByRole("button", { name: "Add context reference" }).click();
  await page
    .getByLabel("Synthetic observation 1", { exact: true })
    .fill(
      "Synthetic human observation attached to this frozen workforce context " +
        suffix,
    );
  const [r] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/review") &&
        r.request().method() === "POST",
    ),
    page
      .getByLabel("Packet action reason")
      .fill("Record reasoned human observations " + suffix),
  ]);
  expect(r.status()).toBe(200);
  const packet = await r.json();
  await expect(
    page.getByText(
      `Saved on server · revision ${packet.revision} · version ${packet.version}.`,
      { exact: true },
    ),
  ).toBeVisible();
  return packet;
}
async function submit(page: Page) {
  await page
    .getByLabel("Packet action reason")
    .fill("Submit this saved human review");
  return postClick(page, "submit", "Submit human review");
}
async function calibrate(page: Page, employee: string, edit = false) {
  await open(page, employee);
  await page
    .getByLabel("Packet action reason")
    .fill("Begin independent human calibration");
  await postClick(page, "start-calibration", "Begin calibration");
  if (edit) {
    await page
      .getByLabel("Accomplishments", { exact: true })
      .fill("Synthetic observation clarified during independent calibration");
    await page
      .getByLabel("Packet action reason")
      .fill("Clarify observable evidence and preserve before/after lineage");
    const revision = await postClick(
      page,
      "calibration",
      "Save calibration revision",
    );
    expect(revision.calibrations).toHaveLength(revision.revision === 3 ? 1 : 2);
    const change = revision.calibrations.at(-1);
    expect(change.before_sha256).not.toBe(change.after_sha256);
  }
  await page
    .getByLabel("Packet action reason")
    .fill("Attended calibration and reviewed this exact revision");
  await page
    .getByLabel(
      "I reviewed this exact revision and acknowledge my human review responsibility.",
    )
    .check();
  const packet = await postClick(
    page,
    "ack-calibration",
    "Acknowledge calibration",
  );
  await expect(
    page.getByRole("heading", { name: "Signatures on this revision: 1" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close packet", exact: true }).click();
  return packet;
}
async function hrApproveKeyboard(page: Page, employee: string) {
  await open(page, employee);
  const reason = page.getByLabel("Packet action reason");
  await reason.fill("Independent human HR review of this exact revision");
  await reason.focus();
  await page.keyboard.press("Tab");
  const ack = page.getByLabel(
    "I reviewed this exact revision and acknowledge my human review responsibility.",
  );
  await expect(ack).toBeFocused();
  await page.keyboard.press("Space");
  await expect(ack).toBeChecked();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Approve reviewed packet", exact: true }),
  ).toBeFocused();
  const [r] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/approve") &&
        r.request().method() === "POST",
    ),
    page.keyboard.press("Enter"),
  ]);
  expect(r.status()).toBe(200);
  await expect(
    page.getByRole("heading", { name: "Signatures on this revision: 2" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close packet", exact: true }).click();
  return await r.json();
}
async function download(page: Page) {
  await expect(
    page.getByRole("button", { name: "Download review JSON", exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", { name: "Download review JSON", exact: true })
      .click(),
  ]);
  const data = await readFile((await download.path())!);
  return { data, document: JSON.parse(data.toString("utf8")) };
}
async function screenshot(page: Page, name: string) {
  const packet = page.locator(".talent-packet");
  if (await packet.count())
    await packet.evaluate((e) => e.scrollIntoView({ block: "start" }));
  await page.screenshot({
    path: `docs/evidence/p06-${test.info().project.name}-${name}.png`,
  });
}
test("human narrative calibration, HR return, signatures, lock, worker recovery and immutable reopen", async ({
  page,
}, info) => {
  const title =
    "Human review " + info.project.name + " " + randomUUID().slice(0, 8);
  await studio(page, "hr");
  const cycle = await create(page, title);
  await studio(page, "manager-a", title);
  const one = await open(page, "SYN-000025");
  await expect(page.locator(".talent-context")).toContainText(
    one.context.grade,
  );
  await expect(page.locator(".talent-context")).not.toContainText("undefined");
  const saved = await saveAuthor(page, "first authored revision");
  expect(saved.revision).toBe(2);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await screenshot(page, "author");
  await submit(page);
  await studio(page, "manager-b", title);
  const two = await open(page, "SYN-000049");
  await saveAuthor(page, "peer team authored revision");
  await submit(page);
  await studio(page, "approver", title);
  await calibrate(page, "SYN-000025", true);
  await calibrate(page, "SYN-000049");
  await studio(page, "hr", title);
  await open(page, "SYN-000025");
  await page
    .getByLabel("Packet action reason")
    .fill("Return for clearer source citations");
  const returned = await postClick(page, "return", "Return for revision");
  expect(returned.revision).toBe(3);
  expect(returned.state).toBe("RETURNED");
  await screenshot(page, "returned");
  await studio(page, "manager-a", title);
  await open(page, "SYN-000025");
  const revised = await saveAuthor(page, "corrected after HR return");
  expect(revised.revision).toBe(4);
  await submit(page);
  await studio(page, "approver", title);
  await calibrate(page, "SYN-000025");
  await studio(page, "hr", title);
  await hrApproveKeyboard(page, "SYN-000025");
  await hrApproveKeyboard(page, "SYN-000049");
  await page
    .getByLabel("Cycle action reason")
    .fill("Lock the exact independently signed packets");
  const resume = await pauseWorker("outbox");
  try {
    const locked = await postClick(page, "lock", "Lock signed review");
    expect(locked.lock_number).toBe(1);
    await expect(
      page.getByText(
        "Signed packets are locked. Independent export verification is pending.",
        { exact: true },
      ),
    ).toBeVisible();
    expect(
      (await page.request.get(`${root}/cycles/${cycle.id}/export`)).status(),
    ).toBe(409);
    await screenshot(page, "worker-pending");
  } finally {
    await resume();
  }
  const first = await download(page);
  expect(first.document.manifest.row_count).toBe(2);
  expect(
    first.document.manifest.rows.map(
      (r: { revision: { number: number } }) => r.revision.number,
    ),
  ).toEqual([4, 2]);
  expect(
    first.document.manifest.rows.every(
      (r: { signatures: unknown[] }) => r.signatures.length === 2,
    ),
  ).toBe(true);
  await screenshot(page, "locked-export");
  await page
    .getByLabel("Cycle action reason")
    .fill("Append a new reviewed correction preserving the old export");
  await postClick(page, "reopen", "Reopen packet SYN-000025");
  await expect(
    page.locator(".talent-queue tbody tr").filter({ hasText: "SYN-000025" }),
  ).toContainText("DRAFT");
  await expect(
    page.getByText("Historical locked export · 2 packets", { exact: true }),
  ).toBeVisible();
  expect((await download(page)).data.equals(first.data)).toBe(true);
  await studio(page, "manager-a", title);
  const reopened = await open(page, "SYN-000025");
  expect(reopened.revision).toBe(5);
  expect(reopened.review).toEqual(revised.review);
  await page.getByLabel("View immutable revision").selectOption("4");
  await expect(
    page.getByText(
      "Historical immutable revision. New signatures and edits bind the current revision only.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(
    await page
      .getByRole("textbox", { name: "Accomplishments", exact: true })
      .count(),
  ).toBe(0);
  await screenshot(page, "historical");
  await page
    .getByRole("button", { name: "Reload current revision", exact: true })
    .click();
  await expect(
    page.getByLabel("Accomplishments", { exact: true }),
  ).toBeVisible();
  const resaved = await saveAuthor(page, "new reviewed correction");
  expect(resaved.revision).toBe(6);
  await submit(page);
  await studio(page, "approver", title);
  await calibrate(page, "SYN-000025", true);
  await studio(page, "hr", title);
  await hrApproveKeyboard(page, "SYN-000025");
  await page
    .getByLabel("Cycle action reason")
    .fill("Lock version two with new current signatures");
  const secondLock = await postClick(page, "lock", "Lock signed review");
  expect(secondLock.lock_number).toBe(2);
  const second = await download(page);
  expect(second.document.manifest.lock_number).toBe(2);
  expect(
    second.document.manifest.rows.map(
      (r: { revision: { number: number } }) => r.revision.number,
    ),
  ).toEqual([7, 2]);
  expect(second.data.equals(first.data)).toBe(false);
  await page.getByLabel("Export lock version").fill("1");
  await expect(
    page.getByText("Historical locked export · 2 packets", { exact: true }),
  ).toBeVisible();
  expect((await download(page)).data.equals(first.data)).toBe(true);
  await studio(page, "auditor");
  await expect(page.locator(".talent-evidence-row").first()).toBeVisible();
  const evidence = page
    .locator(".talent-evidence-row")
    .filter({ hasText: cycle.id });
  for (let n = 0; n < 100 && !(await evidence.count()); n++) {
    const next = page.getByRole("button", {
      name: "Next evidence page",
      exact: true,
    });
    await expect(next).toBeEnabled();
    const [r] = await Promise.all([
      page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === root + "/evidence" &&
          r.request().method() === "GET",
      ),
      next.click(),
    ]);
    expect(r.status()).toBe(200);
    const items = (await r.json()).items;
    if (items.length)
      await expect(
        page.locator(".talent-evidence-row").first(),
      ).toHaveAttribute("data-cycle-id", items[0].cycle_id);
  }
  await expect(evidence).toContainText("Artifact signature verified");
  await expect(evidence).not.toContainText("SYN-");
  await evidence.evaluate((element) =>
    element.scrollIntoView({ block: "start" }),
  );
  await screenshot(page, "auditor");
  expect((await page.request.get(`${root}/packets/${one.id}`)).status()).toBe(
    403,
  );
  expect(
    (await page.request.get(`${root}/cycles/${cycle.id}/export`)).status(),
  ).toBe(403);
  await studio(page, "manager-a", title);
  expect((await page.request.get(`${root}/packets/${two.id}`)).status()).toBe(
    404,
  );
  expect(
    (await page.request.get(`${root}/packets/${two.id}?revision=2`)).status(),
  ).toBe(404);
  expect(
    (await page.request.get(`${root}/cycles/${cycle.id}/export`)).status(),
  ).toBe(403);
  const lockedPacket = await open(page, "SYN-000025");
  expect(lockedPacket.state).toBe("LOCKED");
  const identity = await page.request.get("/api/v1/me");
  const csrf = (await identity.json()).csrf_token;
  const forbidden = await page.request.post(
    `${root}/packets/${one.id}/review`,
    {
      data: {
        review: lockedPacket.review,
        reason: "Attempt a locked mutation",
      },
      headers: {
        "X-CSRF-Token": csrf,
        Origin: "http://127.0.0.1:3017",
        "Idempotency-Key": randomUUID(),
        "If-Match": String(lockedPacket.version),
      },
    },
  );
  expect(forbidden.status()).toBe(409);
  await page.setViewportSize({ width: 390, height: 844 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(391);
  await screenshot(page, "mobile-locked");
  await writeFile(
    `docs/evidence/p06-${info.project.name}-journey.json`,
    JSON.stringify(
      {
        source: "real persisted local API/worker/browser",
        cycle_id: cycle.id,
        snapshot_id: cycle.snapshot_id,
        policy_id: cycle.policy_id,
        packet_count: 2,
        first_lock_revisions: [4, 2],
        second_lock_revisions: [7, 2],
        human_signatures_per_lock: 4,
        old_export_byte_identical: true,
        calibration_before_after_verified: true,
        locked_mutation_status: 409,
        peer_packet_status: 404,
        auditor_raw_status: 403,
      },
      null,
      2,
    ) + "\n",
  );
});
test("optimistic conflict retains a browser draft and restricted personas receive no narrative", async ({
  page,
}, info) => {
  await studio(page, "hr");
  const title =
    "Conflict review " + info.project.name + " " + randomUUID().slice(0, 8);
  const cycle = await create(page, title);
  await studio(page, "manager-a", title);
  const packet = await open(page, "SYN-000025");
  await page.getByLabel("Autosave author drafts after a pause").uncheck();
  await page
    .getByLabel("Accomplishments", { exact: true })
    .fill("Retain this unsaved synthetic browser observation");
  await page
    .getByLabel("Packet action reason")
    .fill("Reasoned conflict recovery");
  const identity = await page.request.get("/api/v1/me");
  const csrf = (await identity.json()).csrf_token;
  const concurrent = await page.request.post(
    `${root}/packets/${packet.id}/review`,
    {
      data: {
        review: {
          ...packet.review,
          accomplishments: "Other session persisted this observation",
        },
        reason: "Concurrent persisted revision",
      },
      headers: {
        "X-CSRF-Token": csrf,
        Origin: "http://127.0.0.1:3017",
        "Idempotency-Key": randomUUID(),
        "If-Match": String(packet.version),
      },
    },
  );
  expect(concurrent.status()).toBe(200);
  const [failed] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith("/review") &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", { name: "Save retained draft", exact: true })
      .click(),
  ]);
  expect(failed.status()).toBe(409);
  await expect(page.locator(".talent-packet [role=alert]")).toContainText(
    "Review changed",
  );
  await expect(page.getByLabel("Accomplishments", { exact: true })).toHaveValue(
    "Retain this unsaved synthetic browser observation",
  );
  await screenshot(page, "conflict");
  const [reloaded] = await Promise.all([
    page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === root + "/packets/" + packet.id &&
        r.request().method() === "GET",
    ),
    page
      .getByRole("button", {
        name: "Reload current revision (keep draft)",
        exact: true,
      })
      .click(),
  ]);
  expect(reloaded.status()).toBe(200);
  const currentVersion = (await reloaded.json()).version;
  await expect(page.locator(".talent-packet .panel-heading p")).toContainText(
    `server version ${currentVersion}`,
  );
  await expect(page.getByLabel("Accomplishments", { exact: true })).toHaveValue(
    "Retain this unsaved synthetic browser observation",
  );
  const saved = await postClick(page, "review", "Save retained draft");
  expect(saved.revision).toBe(3);
  expect(saved.review.accomplishments).toBe(
    "Retain this unsaved synthetic browser observation",
  );
  await expect(
    page.getByText(
      `Saved on server · revision ${saved.revision} · version ${saved.version}.`,
      { exact: true },
    ),
  ).toBeVisible();
  for (const persona of ["finance", "executive", "steward"]) {
    await studio(page, persona);
    expect(
      (await page.request.get(`${root}/packets/${packet.id}`)).status(),
    ).toBe(403);
    expect(
      (await page.request.get(`${root}/cycles/${cycle.id}/export`)).status(),
    ).toBe(403);
    expect(await page.locator("body").innerText()).not.toContain(
      "Other session persisted this observation",
    );
    expect(await page.locator("body").innerText()).not.toContain(
      "Retain this unsaved synthetic browser observation",
    );
  }
  await page.setViewportSize({ width: 390, height: 844 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await screenshot(page, "restricted");
});
