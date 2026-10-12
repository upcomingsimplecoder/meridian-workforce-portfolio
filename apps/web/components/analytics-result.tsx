"use client";
import { useState, type KeyboardEvent } from "react";
import Link from "next/link";
import { replayHref } from "@/lib/replay";
import type { AnalyticalCommand, AnalyticalRun } from "@/lib/analytics";
import { money } from "@/lib/scenarios";

function date(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : "No successful sync";
}
function horizontalNavigation(event: KeyboardEvent<HTMLDivElement>) {
  if (
    event.target !== event.currentTarget ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  )
    return;
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    event.preventDefault();
    event.currentTarget.scrollBy({
      left: event.key === "ArrowRight" ? 140 : -140,
      behavior: "auto",
    });
  }
}
export function AnalyticsRunResult({
  run,
  busy,
  command,
  download,
}: {
  run: AnalyticalRun;
  busy: boolean;
  command: (value: AnalyticalCommand) => Promise<unknown>;
  download: () => Promise<unknown>;
}) {
  const [reason, setReason] = useState("");
  const [eventId, setEventId] = useState("");
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const mismatches = run.comparisons.filter((row) => !row.matches);
  async function act(value: AnalyticalCommand) {
    setError("");
    try {
      await command(value);
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "The action was not acknowledged.",
      );
    }
  }
  return (
    <section
      className="panel analytical-result"
      aria-label="Selected analytical run"
      data-run-id={run.id}
      aria-busy={busy}
    >
      <div className="panel-heading">
        <div>
          <span className="eyebrow">{run.kind} · IMMUTABLE VERSION</span>
          <h2>{run.snapshot_title}</h2>
          <p role="status">
            {run.status} · {run.pipeline_status}
          </p>
        </div>
        <span className={`analytical-state state-${run.status.toLowerCase()}`}>
          {run.status === "PASS"
            ? "Reconciled"
            : run.status === "DRIFT"
              ? "Publication blocked"
              : run.status === "FAILED"
                ? "Job exhausted"
                : "Worker pending"}
        </span>
      </div>
      {run.historical && (
        <p className="analytical-notice">
          Historical result. A newer source or analytical attempt exists; these
          totals describe only the versions shown below.
        </p>
      )}
      {run.pipeline_status === "OUTAGE" && (
        <p className="analytical-notice" role="alert">
          The analytical pipeline is unavailable. Any displayed totals remain
          the results of this pinned run. Check the last successful sync and
          worker last seen below.
        </p>
      )}
      {run.status === "DRIFT" && (
        <p className="error-box">
          {mismatches.length} count, sum or hash comparisons differ. This result
          is not published. The last good publication remains available.
        </p>
      )}
      {run.status === "QUEUED" && (
        <p>
          SQL results are pending. No totals are shown until the independent
          worker acknowledges this exact run.
        </p>
      )}
      <dl className="analytical-metadata">
        <dt>Run</dt>
        <dd>{run.id}</dd>
        <dt>Business as-of</dt>
        <dd>{run.data_as_of}</dd>
        <dt>Source watermark</dt>
        <dd>{run.source_watermark}</dd>
        <dt>Source batch</dt>
        <dd>{run.source_batch_id}</dd>
        <dt>Last successful sync</dt>
        <dd>{date(run.last_successful_sync)}</dd>
        <dt>Worker last seen</dt>
        <dd>{date(run.worker_last_seen)}</dd>
        <dt>Model</dt>
        <dd>{run.model_version}</dd>
        <dt>Cloud validation</dt>
        <dd>UNVERIFIED</dd>
        <dt>Input SHA-256</dt>
        <dd>{run.inputs_sha256}</dd>
        <dt>Model SHA-256</dt>
        <dd>{run.model_sha256}</dd>
        <dt>Result SHA-256</dt>
        <dd>{run.result_sha256 || "Pending"}</dd>
        {run.parent_run_id && (
          <>
            <dt>Prior run</dt>
            <dd>{run.parent_run_id}</dd>
          </>
        )}
      </dl>
      {run.headcount !== null && (
        <div className="analytical-kpis">
          <article>
            <span>SQL headcount</span>
            <strong>{run.headcount.toLocaleString()}</strong>
          </article>
          <article>
            <span>SQL FTE</span>
            <strong>{run.fte}</strong>
          </article>
          <article>
            <span>Historical intervals</span>
            <strong>{run.employee_versions?.toLocaleString()}</strong>
          </article>
          <article>
            <span>Reconciliation</span>
            <strong>
              {run.comparisons.length - mismatches.length}/
              {run.comparisons.length}
            </strong>
          </article>
        </div>
      )}
      {!!run.compensation.length && (
        <div
          className="table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Compensation SQL totals"
          onKeyDown={horizontalNavigation}
        >
          <table>
            <caption>Locked compensation · original currency</caption>
            <thead>
              <tr>
                <th scope="col">Cycle / currency</th>
                <th scope="col">Rows</th>
                <th scope="col">Annualized merit</th>
                <th scope="col">First-year merit</th>
                <th scope="col">Bonus</th>
                <th scope="col">Pool charge</th>
                <th scope="col">Budget</th>
                <th scope="col">Holdback</th>
                <th scope="col">Remaining</th>
              </tr>
            </thead>
            <tbody>
              {run.compensation.map((row) => (
                <tr key={row.cycle_id + row.currency}>
                  <th scope="row">
                    <span className="analytical-id">{row.cycle_id}</span>
                    {row.currency}
                  </th>
                  <td>{row.row_count}</td>
                  <td>{money(row.annualized_merit_minor, row)}</td>
                  <td>{money(row.merit_first_year_minor, row)}</td>
                  <td>{money(row.bonus_payable_minor, row)}</td>
                  <td>{money(row.pool_charge_minor, row)}</td>
                  <td>{money(row.budget_minor, row)}</td>
                  <td>{money(row.holdback_minor, row)}</td>
                  <td>{money(row.remaining_minor, row)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!run.scenarios.length && (
        <div
          className="table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Scenario SQL costs"
          onKeyDown={horizontalNavigation}
        >
          <table>
            <caption>Scenario cost · 12-month calculated totals</caption>
            <thead>
              <tr>
                <th scope="col">Revision</th>
                <th scope="col">Year</th>
                <th scope="col">Annual cost</th>
                <th scope="col">Pinned baseline</th>
                <th scope="col">Delta</th>
                <th scope="col">FX / policy</th>
              </tr>
            </thead>
            <tbody>
              {run.scenarios.map((row) => (
                <tr key={row.revision_id}>
                  <th scope="row">
                    <span className="analytical-id">{row.revision_id}</span>
                  </th>
                  <td>{row.year}</td>
                  <td>{money(row.annual_minor, row)}</td>
                  <td>{money(row.baseline_minor, row)}</td>
                  <td>
                    <Link
                      href={replayHref("SCENARIO", row.revision_id)}
                      aria-label={`Replay scenario cost delta ${row.revision_id}`}
                    >
                      {money(row.delta_minor, row)}
                    </Link>
                  </td>
                  <td>
                    <span className="analytical-id">{row.fx_set_id}</span>
                    <span className="analytical-id">{row.policy_id}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!run.comparisons.length && (
        <details open={run.status === "DRIFT"}>
          <summary>
            Count, sum and semantic-hash comparisons ({run.comparisons.length})
          </summary>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Reconciliation comparisons"
            onKeyDown={horizontalNavigation}
          >
            <table>
              <thead>
                <tr>
                  <th scope="col">Dataset / field</th>
                  <th scope="col">Expected</th>
                  <th scope="col">SQL observed</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {run.comparisons.map((row) => (
                  <tr
                    className={row.matches ? "" : "analytical-mismatch"}
                    key={row.dataset + row.key + row.field}
                  >
                    <th scope="row">
                      {row.dataset} / {row.field}
                      <span className="analytical-id">{row.key}</span>
                    </th>
                    <td className="analytical-hash">{row.expected}</td>
                    <td className="analytical-hash">{row.observed}</td>
                    <td>{row.matches ? "MATCH" : "DRIFT"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      {!!run.artifacts.length && (
        <details>
          <summary>Versioned artifacts ({run.artifacts.length})</summary>
          <ul>
            {run.artifacts.map((row) => (
              <li key={row.name}>
                <strong>{row.name}</strong> · {row.row_count} rows · schema{" "}
                {row.schema_version}
                <span className="analytical-id">SHA-256 {row.sha256}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {run.status === "PASS" || run.status === "DRIFT" ? (
        <button
          className="button"
          disabled={downloading}
          onClick={async () => {
            setError("");
            setDownloading(true);
            try {
              await download();
            } catch (failure) {
              setError(
                failure instanceof Error
                  ? failure.message
                  : "The manifest is unavailable.",
              );
            } finally {
              setDownloading(false);
            }
          }}
        >
          Download verified analytical manifest
        </button>
      ) : null}
      <form
        className="analytical-actions"
        onSubmit={(event) => event.preventDefault()}
      >
        <fieldset disabled={busy}>
          <legend>Reconciliation operations</legend>
          <label>
            Operation reason
            <textarea
              aria-label="Operation reason"
              maxLength={240}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {run.status === "PASS" && !!run.delivery_event_ids.length && (
            <>
              <label>
                Delivery to omit in a new drill
                <select
                  aria-label="Delivery to omit in a new drill"
                  value={eventId}
                  onChange={(event) => setEventId(event.target.value)}
                >
                  <option value="">Choose a real locked-cycle event</option>
                  {run.delivery_event_ids.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              </label>
              <p>
                The synthetic drill creates a new analytical version and leaves
                the locked compensation source intact.
              </p>
              <button
                className="button"
                disabled={reason.trim().length < 3 || !eventId}
                onClick={() =>
                  act({
                    action: "gap-drill",
                    event_id: eventId,
                    reason: reason.trim(),
                  })
                }
              >
                Simulate missed delivery
              </button>
            </>
          )}
          {run.status === "DRIFT" && (
            <button
              className="button primary"
              disabled={reason.trim().length < 3}
              onClick={() => act({ action: "repair", reason: reason.trim() })}
            >
              Repair exact pinned inputs
            </button>
          )}
          {run.status === "FAILED" && (
            <>
              <p>
                Attempts: {run.attempts} · {run.job_error}
              </p>
              <button
                className="button primary"
                disabled={reason.trim().length < 3}
                onClick={() => act({ action: "retry", reason: reason.trim() })}
              >
                Retry exhausted analytical job
              </button>
            </>
          )}
        </fieldset>
      </form>
      {error && (
        <p role="alert" className="error-box">
          {error}
        </p>
      )}
    </section>
  );
}
