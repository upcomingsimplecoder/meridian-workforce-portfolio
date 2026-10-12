import { describe, expect, it } from "vitest";
import { canPublish, sourceMedia, type Batch } from "@/lib/ingestion";

// Unit fixtures exercise client affordances. Persisted publication is tested against Postgres and all browsers.
const validated: Batch = {
  id: "synthetic-batch",
  title: "Synthetic source",
  state: "VALIDATED",
  version: 2,
  accepted_count: 5000,
  rejected_count: 0,
  total_count: 5000,
  base_snapshot_id: "base",
  snapshot_id: null,
  supersedes_batch_id: null,
  source_sha256: "0".repeat(64),
  source_watermark: "2027-07-31",
  received_at: "2026-10-09T00:00:00Z",
  validated_at: "2026-10-09T00:00:01Z",
  published_at: null,
  parser_version: "workforce-source-v1",
  pending_job: false,
  failed_job: false,
  manifest_ready: false,
};
describe("source publication affordance", () => {
  it("offers publication only for completed zero-reject validation", () => {
    expect(canPublish(validated)).toBe(true);
    for (const change of [
      { state: "QUARANTINED", rejected_count: 40, accepted_count: 4960 },
      { state: "RECEIVED", total_count: null },
      { state: "PUBLISHED" },
      { pending_job: true },
      { failed_job: true },
      { accepted_count: 0, total_count: 0 },
      { total_count: 5001 },
    ])
      expect(canPublish({ ...validated, ...change })).toBe(false);
  });
});
describe("bounded workforce uploads", () => {
  it.each([
    ["SOURCE.CSV", "text/csv"],
    ["source.json", "application/json"],
  ])("recognizes the explicit format of %s", (name, media) => {
    expect(sourceMedia({ name, size: 1024 })).toBe(media);
  });
  it.each([
    { name: "source.csv", size: 0 },
    { name: "source.csv", size: 8 * 1024 * 1024 + 1 },
    { name: "source.html", size: 20 },
  ])(
    "rejects unsupported or unbounded input %j before allocating bytes",
    (file) => {
      expect(() => sourceMedia(file)).toThrow();
    },
  );
});
