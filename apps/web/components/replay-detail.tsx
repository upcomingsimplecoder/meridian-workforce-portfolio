"use client";
import { useState, type KeyboardEvent } from "react";
import type { Replay } from "@/lib/replay";
import { money } from "@/lib/scenarios";
import { compMoney } from "@/lib/compensation";

export function replayHorizontal(event: KeyboardEvent<HTMLDivElement>) {
  if (
    event.target !== event.currentTarget ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  )
    return;
  if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    event.currentTarget.scrollLeft += event.key === "ArrowRight" ? 140 : -140;
  }
}
function Fingerprint({ value }: { value: string }) {
  return <code className="replay-hash">{value}</code>;
}

export function ReplayDetail({
  replay,
  busy,
  verify,
  download,
}: {
  replay: Replay;
  busy: boolean;
  verify: (reason: string) => Promise<unknown>;
  download: (attemptId: string) => Promise<unknown>;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const aggregate = replay.aggregate;
  const award = replay.award;
  const latest = replay.attempts[0];
  return (
    <section
      className="panel replay-detail"
      aria-label="Selected decision replay"
      data-capsule-id={replay.id}
      aria-busy={busy}
    >
      <div className="panel-heading">
        <div>
          <span className="eyebrow">{replay.kind} · FROZEN EVIDENCE</span>
          <h2>Reconstruct the decision</h2>
          <p>
            Captured {new Date(replay.created_at).toLocaleString()} ·{" "}
            {replay.redacted
              ? "Redacted reviewer view"
              : "Authorized financial view"}
          </p>
        </div>
        <span
          className={`replay-state ${latest.status === "MATCH" ? "match" : "failed"}`}
        >
          {latest.status}
        </span>
      </div>
      <p className="replay-boundary">
        These figures and decisions describe the versions captured here. Later
        corrections and approvals do not replace this evidence. Downloads rerun
        the pinned calculation and recheck current access.
      </p>
      {replay.integrity_issues.length > 0 && (
        <p role="alert" className="error-box">
          Frozen dependencies cannot currently be verified:{" "}
          {replay.integrity_issues.join(" · ")}. Historical figures remain
          evidence of the captured calculation.
        </p>
      )}
      <dl className="replay-metadata">
        <dt>Capsule</dt>
        <dd>{replay.id}</dd>
        <dt>Selected revision</dt>
        <dd>{replay.subject_id}</dd>
        <dt>Source watermark</dt>
        <dd>{replay.source_watermark}</dd>
        <dt>Frozen SHA-256</dt>
        <dd>
          <Fingerprint value={replay.sha256} />
        </dd>
      </dl>
      <ol className="replay-path" aria-label="Decision provenance chain">
        <li>
          <strong>Source</strong>
          <span>{replay.source_lineage.length} captured source versions</span>
        </li>
        <li>
          <strong>Changes</strong>
          <span>{replay.operations.length} effective-dated operations</span>
        </li>
        <li>
          <strong>Calculation</strong>
          <span>{replay.pins.length} explicit pins</span>
        </li>
        <li>
          <strong>Human review</strong>
          <span>
            {replay.decisions.length} decisions · {replay.approval_steps.length}{" "}
            assigned steps
          </span>
        </li>
        <li>
          <strong>Verification</strong>
          <span>
            Attempt {latest.number} · {latest.status}
          </span>
        </li>
      </ol>
      {aggregate && (
        <>
          <div className="scenario-totals">
            <div>
              <span>Pinned baseline</span>
              <strong data-testid="replay-baseline">
                {money(aggregate.baseline.total_minor, aggregate)}
              </strong>
            </div>
            <div>
              <span>Calculated 12-month cost</span>
              <strong data-testid="replay-annual">
                {money(aggregate.planned.total_minor, aggregate)}
              </strong>
            </div>
            <div>
              <span>Exact cost delta</span>
              <strong data-testid="replay-delta">
                {money(aggregate.delta_minor, aggregate)}
              </strong>
            </div>
          </div>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Replay monthly cost table"
            onKeyDown={replayHorizontal}
          >
            <table className="data-table">
              <caption>
                Monthly calculated cost and comparison · {aggregate.currency} ·{" "}
                {aggregate.exponent} decimal places
              </caption>
              <thead>
                <tr>
                  <th scope="col">Month</th>
                  <th scope="col">Baseline total</th>
                  <th scope="col">Base</th>
                  <th scope="col">Benefits</th>
                  <th scope="col">Variable</th>
                  <th scope="col">Employer tax</th>
                  <th scope="col">Planned total</th>
                  <th scope="col">Delta</th>
                </tr>
              </thead>
              <tbody>
                {aggregate.months.map((row) => (
                  <tr key={row.month}>
                    <th scope="row">{row.month}</th>
                    <td>{money(row.baseline.total_minor, aggregate)}</td>
                    <td>{money(row.planned.base_minor, aggregate)}</td>
                    <td>{money(row.planned.benefits_minor, aggregate)}</td>
                    <td>{money(row.planned.variable_minor, aggregate)}</td>
                    <td>{money(row.planned.employer_tax_minor, aggregate)}</td>
                    <td>{money(row.planned.total_minor, aggregate)}</td>
                    <td>{money(row.delta_minor, aggregate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {award && (
        <section className="replay-award">
          <h3>Exact award calculation</h3>
          <dl className="replay-metadata">
            <dt>Employee</dt>
            <dd>{award.employee_id}</dd>
            <dt>Annualized merit</dt>
            <dd>
              {compMoney(
                award.annualized_merit_minor,
                award.currency,
                award.currency_exponent,
              )}
            </dd>
            <dt>First-year merit</dt>
            <dd>
              {compMoney(
                award.merit_first_year_minor,
                award.currency,
                award.currency_exponent,
              )}
            </dd>
            <dt>Bonus payable</dt>
            <dd>
              {compMoney(
                award.bonus_payable_minor,
                award.currency,
                award.currency_exponent,
              )}
            </dd>
            <dt>Pool charge</dt>
            <dd data-testid="replay-award-charge">
              {compMoney(
                award.pool_charge_minor,
                award.currency,
                award.currency_exponent,
              )}
            </dd>
            <dt>Bonus service proration</dt>
            <dd>
              {award.bonus_service_fte_days} FTE days /{" "}
              {award.bonus_service_calendar_days} calendar days
            </dd>
          </dl>
          <details>
            <summary>Monthly merit and pinned compensation dates</summary>
            <dl className="replay-metadata">
              {Object.entries(award.inputs.terms).map(([key, value]) => (
                <div key={key}>
                  <dt>{key.replaceAll("_", " ")}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            <ul>
              {award.merit_months.map((row) => (
                <li key={row.month}>
                  {row.month} ·{" "}
                  {compMoney(
                    row.merit_cost_minor,
                    award.currency,
                    award.currency_exponent,
                  )}
                </li>
              ))}
            </ul>
          </details>
        </section>
      )}
      {!aggregate && !award && (
        <p className="replay-boundary">
          Individual compensation values and personnel narratives are withheld.
          Financial aggregates are also withheld for cohorts smaller than five.
        </p>
      )}
      {replay.operations.length > 0 && (
        <section>
          <h3>Contributing operations</h3>
          <ul className="replay-records">
            {replay.operations.map((row) => (
              <li key={row.id}>
                <strong>
                  {row.kind} · {row.effective_date}
                </strong>
                <span>Operation {row.id}</span>
                <Fingerprint value={row.sha256} />
              </li>
            ))}
          </ul>
          {replay.full_operations.length > 0 && (
            <details>
              <summary>Authorized original operation inputs</summary>
              <pre className="replay-json">
                {JSON.stringify(replay.full_operations, null, 2)}
              </pre>
            </details>
          )}
        </section>
      )}
      <section>
        <h3>Versions used by the calculation</h3>
        <dl className="replay-pins">
          {replay.pins.map((pin) => (
            <div key={pin.name}>
              <dt>{pin.name}</dt>
              <dd>
                {pin.version}
                {pin.sha256 && <Fingerprint value={pin.sha256} />}
              </dd>
            </div>
          ))}
        </dl>
      </section>
      <section>
        <h3>Source lineage</h3>
        <ol className="replay-records">
          {replay.source_lineage.map((row) => (
            <li key={row.batch_id}>
              <strong>
                {row.source_type} · schema {row.schema_version} ·{" "}
                {row.watermark}
              </strong>
              <span>
                Batch {row.batch_id} · {row.accepted_count.toLocaleString()}{" "}
                accepted / {row.rejected_count.toLocaleString()} quarantined
              </span>
              <Fingerprint value={row.sha256} />
              {row.supersedes_batch_id && (
                <span>
                  Corrects {row.supersedes_batch_id} · base snapshot{" "}
                  {row.base_snapshot_id}
                </span>
              )}
            </li>
          ))}
        </ol>
      </section>
      {replay.approval_steps.length > 0 && (
        <section>
          <h3>Frozen approval hierarchy</h3>
          <ol className="replay-records">
            {replay.approval_steps.map((row, index) => (
              <li key={index}>
                <strong>
                  {row.ordinal} · {row.role}
                </strong>
                <span>
                  Assigned reviewer reference {row.assigned_reference} ·{" "}
                  {row.capability}
                </span>
                <span>
                  Required exceptions:{" "}
                  {Array.isArray(row.exceptions)
                    ? row.exceptions.join(" · ") || "None"
                    : row.exceptions}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}
      <section>
        <h3>Human decision history</h3>
        {replay.decisions.length ? (
          <ol className="replay-records">
            {replay.decisions.map((row) => (
              <li key={row.id} data-decision-action={row.action}>
                <strong>
                  {row.action} · step {row.ordinal} ·{" "}
                  {new Date(row.at).toLocaleString()}
                </strong>
                <span>
                  Revision {row.revision_id} · actor reference{" "}
                  {row.actor_reference}
                </span>
                {row.reason && <p>{row.reason}</p>}
                <span>Reason fingerprint</span>
                <Fingerprint value={row.reason_sha256} />
                <span>Correlation {row.correlation_id}</span>
                {row.exceptions.length > 0 && (
                  <span>Acknowledged: {row.exceptions.join(" · ")}</span>
                )}
              </li>
            ))}
          </ol>
        ) : (
          <p>No human approval decisions existed at capture time.</p>
        )}
      </section>
      {replay.award_history.length > 0 && (
        <details>
          <summary>Calculated award differences between revisions</summary>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Replay award revision table"
            onKeyDown={replayHorizontal}
          >
            <table className="data-table">
              <caption>
                Exact original-currency calculated amounts for each immutable
                revision
              </caption>
              <thead>
                <tr>
                  <th scope="col">Revision</th>
                  <th scope="col">Annualized merit</th>
                  <th scope="col">First-year merit</th>
                  <th scope="col">Bonus payable</th>
                  <th scope="col">Pool charge</th>
                </tr>
              </thead>
              <tbody>
                {replay.award_history.map((row) => (
                  <tr key={row.revision_id}>
                    <th scope="row">{row.number}</th>
                    {[
                      row.award.annualized_merit_minor,
                      row.award.merit_first_year_minor,
                      row.award.bonus_payable_minor,
                      row.award.pool_charge_minor,
                    ].map((minor, index) => (
                      <td key={index}>
                        {compMoney(
                          minor,
                          row.award.currency,
                          row.award.currency_exponent,
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      {replay.revision_history.length > 0 && (
        <details>
          <summary>Immutable proposal revisions</summary>
          <ul className="replay-records">
            {replay.revision_history.map((row, index) => (
              <li key={index}>
                <strong>
                  Revision {row.number} · {row.kind}
                </strong>
                <span>{row.id}</span>
                <Fingerprint value={String(row.sha256)} />
              </li>
            ))}
          </ul>
        </details>
      )}
      {replay.hierarchy.length > 0 && (
        <details>
          <summary>Frozen workforce reporting chain</summary>
          <ol className="replay-records">
            {replay.hierarchy.map((row, index) => (
              <li key={index}>
                <span>
                  Person reference {row.person_reference} → manager reference{" "}
                  {row.manager_reference}
                </span>
                <span>
                  Workforce version {row.version_id} · source{" "}
                  {row.source_batch_id}
                </span>
              </li>
            ))}
          </ol>
        </details>
      )}
      <details>
        <summary>
          Chronological event timeline ({replay.timeline.length})
        </summary>
        <ol className="replay-records">
          {replay.timeline.map((row) => (
            <li key={`${row.kind}-${row.id}`}>
              <strong>
                {row.kind} · {row.verb}
              </strong>
              <span>
                {new Date(row.at).toLocaleString()} · correlation{" "}
                {row.correlation_id}
              </span>
              {row.policy_version && (
                <span>Authorization policy {row.policy_version}</span>
              )}
              <Fingerprint value={row.sha256} />
            </li>
          ))}
        </ol>
      </details>
      <form
        className="replay-verify"
        onSubmit={async (event) => {
          event.preventDefault();
          setError("");
          try {
            await verify(reason.trim());
          } catch (failure) {
            setError(
              failure instanceof Error
                ? failure.message
                : "Verification was not acknowledged.",
            );
          }
        }}
      >
        <h3>Verify the frozen calculation</h3>
        <label>
          Verification reason
          <textarea
            aria-label="Verification reason"
            required
            minLength={3}
            maxLength={240}
            value={reason}
            disabled={busy}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button
          className="button primary"
          disabled={busy || reason.trim().length < 3}
        >
          {busy ? "Verifying pinned inputs…" : "Run deterministic verification"}
        </button>
      </form>
      {error && (
        <p role="alert" className="error-box">
          {error}
        </p>
      )}
      <section>
        <h3>Immutable verification attempts</h3>
        <p>
          The latest 25 attempts are shown. A failed attempt does not erase an
          earlier result. SHA-256 verifies reproducibility; the local HMAC
          authenticates the exported bytes. There is no externally signed daily
          digest or tamper-proof guarantee.
        </p>
        <div
          className="table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Replay verification attempt table"
          onKeyDown={replayHorizontal}
        >
          <table className="data-table">
            <caption>Persisted verification results</caption>
            <thead>
              <tr>
                <th scope="col">Attempt</th>
                <th scope="col">Result</th>
                <th scope="col">Observed SHA-256</th>
                <th scope="col">Dependency issues</th>
                <th scope="col">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {replay.attempts.map((row) => (
                <tr key={row.id} data-attempt-number={row.number}>
                  <th scope="row">{row.number}</th>
                  <td>{row.status}</td>
                  <td>
                    <Fingerprint
                      value={row.observed_sha256 || "No reproducible result"}
                    />
                  </td>
                  <td>
                    {[...row.issues, ...(row.artifact_issues || [])].join(
                      " · ",
                    ) || "None"}
                  </td>
                  <td>
                    <button
                      className="button"
                      disabled={busy || downloading || !row.export_ready}
                      onClick={async () => {
                        setError("");
                        setDownloading(true);
                        try {
                          await download(row.id);
                        } catch (failure) {
                          setError(
                            failure instanceof Error
                              ? failure.message
                              : "Evidence download failed.",
                          );
                        } finally {
                          setDownloading(false);
                        }
                      }}
                    >
                      Download redacted evidence {row.number}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}
