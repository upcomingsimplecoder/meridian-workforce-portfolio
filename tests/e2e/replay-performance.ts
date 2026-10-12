import { expect, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import type { components } from "../../packages/contracts/api";
import { measurementEnvironment } from "./environment";
type Replay = components["schemas"]["ReplayView"];
export async function measureReplayReads(
  page: Page,
  replay: Replay,
  browser: string,
) {
  const readings = [];
  for (const path of [
    "/api/v1/replay/capsules",
    `/api/v1/replay/capsules/${replay.id}`,
    "/api/v1/replay/audit",
  ]) {
    const samples = [];
    let bytes = 0;
    for (let i = 0; i < 23; i++) {
      const start = performance.now();
      const response = await page.request.get(path);
      expect(response.status()).toBe(200);
      bytes = Math.max(bytes, (await response.body()).length);
      const elapsed = performance.now() - start;
      if (i >= 3) samples.push(elapsed);
    }
    samples.sort((a, b) => a - b);
    readings.push({
      path,
      samples: 20,
      warmups: 3,
      p50_ms: Math.round(samples[9] * 1000) / 1000,
      p95_ms: Math.round(samples[18] * 1000) / 1000,
      max_ms: Math.round(samples[19] * 1000) / 1000,
      maximum_response_bytes: bytes,
      errors: 0,
    });
  }
  await writeFile(
    `docs/evidence/p08-${browser}-api-performance.json`,
    JSON.stringify(
      {
        scope:
          "warm authenticated browser HTTP reads on the observed test stack; deployed demo, cold, login and throughput UNVERIFIED",
        date: new Date().toISOString(),
        browser,
        hardware: measurementEnvironment(),
        request_stream_concurrency: 1,
        background:
          "Normal UI queries and idle independently runnable workers remain enabled",
        dataset: {
          source_people: 5000,
          source_history_intervals: 5002,
          forecast_months: 12,
          operations: 1,
          captured_human_decisions: replay.decisions.length,
          captured_event_anchors: replay.timeline.length,
          list_limit: 20,
          audit_limit: 25,
        },
        readings,
      },
      null,
      2,
    ) + "\n",
  );
}
