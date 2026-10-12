import { expect, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { cpus, totalmem, platform, arch, release } from "node:os";
import type { components } from "../../packages/contracts/api";
import { measurementEnvironment } from "./environment";

type Run = components["schemas"]["AnalyticsRunView"];

export async function measureAnalyticalReads(
  page: Page,
  run: Run,
  browser: string,
) {
  const routes = [
    ["selected_run", `/api/v1/analytics/runs/${run.id}`],
    ["bounded_list", "/api/v1/analytics/runs?limit=10"],
    ["verified_manifest", `/api/v1/analytics/runs/${run.id}/manifest`],
    [
      "source_options",
      "/api/v1/analytics/options?source_search=June%20workforce",
    ],
  ];
  const measurements = [];
  for (const [name, path] of routes) {
    const timings: number[] = [];
    const sizes: number[] = [];
    for (let sample = -3; sample < 20; sample++) {
      const begin = performance.now();
      const response = await page.request.get(path);
      const body = await response.body();
      const elapsed = performance.now() - begin;
      expect(response.status()).toBe(200);
      if (name === "selected_run") {
        const actual: Run = JSON.parse(body.toString("utf8"));
        expect(actual.result_sha256).toBe(run.result_sha256);
        expect(actual.scenarios[0].delta_minor).toBe("14048276");
        expect(actual.compensation[0].pool_charge_minor).toBe("1520000");
      }
      if (sample >= 0) {
        timings.push(elapsed);
        sizes.push(body.length);
      }
    }
    const sorted = [...timings].sort((a, b) => a - b);
    const p95 = sorted[Math.ceil(sorted.length * 0.95) - 1];
    measurements.push({
      name,
      measured_samples: 20,
      warmups: 3,
      p95_ms: Number(p95.toFixed(3)),
      min_ms: Number(sorted[0].toFixed(3)),
      max_ms: Number(sorted.at(-1)!.toFixed(3)),
      max_response_bytes: Math.max(...sizes),
    });
  }
  await writeFile(
    `docs/evidence/p07-${browser}-api-performance.json`,
    JSON.stringify(
      {
        claim_scope:
          "Warm authenticated HTTP reads using actual OIDC browser cookies; concurrency one; execution recorded below; no cold/login-throughput claim",
        execution_context: measurementEnvironment(),
        measured_at: new Date().toISOString(),
        browser,
        hardware: {
          cpu: cpus()[0].model,
          cores: cpus().length,
          memory_bytes: totalmem(),
          platform: platform(),
          arch: arch(),
          release: release(),
        },
        dataset: {
          people: 5000,
          historical_intervals: 5002,
          compensation_rows: 3,
          scenario_revisions: 1,
          months_per_scenario: 12,
          comparisons: run.comparisons.length,
        },
        run_id: run.id,
        result_sha256: run.result_sha256,
        measurements,
        read_budget_ms: 400,
        cloud: "UNVERIFIED",
      },
      null,
      2,
    ) + "\n",
  );
  for (const measured of measurements)
    expect(measured.p95_ms, `${measured.name} warm p95`).toBeLessThan(400);
}
