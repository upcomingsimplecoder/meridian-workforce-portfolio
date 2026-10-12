/** Incremental diagnostics without assertion values, tokens, headers or raw reports. */
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync, renameSync } from "node:fs";
import { relative, resolve } from "node:path";

const browsers = new Set(["chromium", "firefox", "webkit"]);
const statuses = new Set([
  "passed",
  "failed",
  "timedOut",
  "skipped",
  "interrupted",
]);
export default class EvidenceReporter {
  document = { complete: false, cases: [] };
  path = resolve(".local/e2e-case-results.json");
  onBegin(config, suite) {
    this.document.started_at = new Date().toISOString();
    this.document.declared_cases = suite.allTests().length;
    mkdirSync(resolve(".local"), { recursive: true, mode: 0o700 });
    this.save();
  }
  onTestEnd(test, result) {
    const file = relative(process.cwd(), test.location.file).replaceAll(
      "\\",
      "/",
    );
    const browser = test.parent.project()?.name;
    const diagnostic = [];
    for (const error of result.errors || []) {
      const frames = [];
      for (const match of (error.stack || "").matchAll(
        /[/\\]tests[/\\]e2e[/\\]([a-z0-9_-]+(?:\.spec)?\.ts):(\d+):(\d+)/g,
      ))
        frames.push({
          file: `tests/e2e/${match[1]}`,
          line: Number(match[2]),
          column: Number(match[3]),
        });
      diagnostic.push({
        category:
          error.message?.includes("Timeout") ||
          error.message?.includes("timeout")
            ? "timeout"
            : error.message?.includes("expect(")
              ? "assertion"
              : "execution",
        frames: frames.slice(0, 8),
      });
    }
    const record = {
      case_sha256: createHash("sha256")
        .update(`${file}\0${test.title}`)
        .digest("hex"),
      file: /^tests\/e2e\/[a-z0-9_-]+\.spec\.ts$/.test(file)
        ? file
        : "[unrecognized]",
      line: test.location.line,
      browser: browsers.has(browser) ? browser : "[unrecognized]",
      status: statuses.has(result.status) ? result.status : "[unrecognized]",
      retry: result.retry,
      duration_ms: result.duration,
      diagnostic,
    };
    this.document.cases.push(record);
    this.save();
    process.stdout.write(`MERIDIAN_CASE ${JSON.stringify(record)}\n`);
  }
  onEnd(result) {
    this.document.complete = true;
    this.document.result = result.status;
    this.document.finished_at = new Date().toISOString();
    this.save();
  }
  save() {
    const temporary = `${this.path}.tmp`;
    writeFileSync(temporary, JSON.stringify(this.document), { mode: 0o600 });
    renameSync(temporary, this.path);
  }
}
