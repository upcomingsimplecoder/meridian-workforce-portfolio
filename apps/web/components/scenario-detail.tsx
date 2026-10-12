"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  downloadScenario,
  getForecast,
  getScenario,
  type Command,
  type Scenario,
  type ScenarioOptions,
} from "@/lib/scenarios";
import { ScenarioForecast } from "./scenario-forecast";
import { ScenarioOperation } from "./scenario-operation";

export function ScenarioDetail({
  id,
  me,
  options,
  pending,
  onCommand,
}: {
  id: string;
  me: Me;
  options: ScenarioOptions;
  pending: boolean;
  onCommand: (scenario: Scenario, command: Command) => void;
}) {
  const [revision, setRevision] = useState<number>();
  const [fork, setFork] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const query = useQuery({
    queryKey: ["scenarios", me.id, "detail", id],
    queryFn: () => getScenario(id),
    refetchInterval: (q) => (q.state.data?.pending_job ? 1000 : false),
  });
  const forecast = useQuery({
    queryKey: ["scenarios", me.id, "forecast", id, revision],
    queryFn: () => getForecast(id, revision),
    refetchInterval: query.data?.pending_job ? 1000 : false,
  });
  if (query.isPending)
    return (
      <section className="panel empty-state" role="status">
        Loading the persisted plan…
      </section>
    );
  if (query.error)
    return (
      <section className="error-box" role="alert">
        {query.error.message}
        <button className="button" onClick={() => query.refetch()}>
          Reload plan
        </button>
      </section>
    );
  const scenario = query.data;
  const editable =
    scenario.owner_id === me.id &&
    ["DRAFT", "REJECTED"].includes(scenario.state);
  const reviewer = scenario.reviewer_id === me.id;
  async function download(kind: "csv" | "manifest") {
    setDownloadError("");
    try {
      await downloadScenario(scenario, revision || scenario.revision, kind);
    } catch (error) {
      setDownloadError(
        error instanceof Error ? error.message : "Download failed.",
      );
    }
  }
  return (
    <section className="panel scenario-detail" aria-label="Selected plan">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">
            REVISION {scenario.revision} · VERSION {scenario.version}
          </span>
          <h2>{scenario.title}</h2>
        </div>
        <span className="status-tag" data-testid="plan-state">
          {scenario.state}
        </span>
      </div>
      <div className="scenario-toolbar">
        <button
          className="button"
          onClick={() => setFork((prev) => !prev)}
          disabled={pending}
        >
          Fork plan
        </button>
        <button
          className="button"
          onClick={() => {
            query.refetch();
            forecast.refetch();
          }}
        >
          Refresh plan
        </button>
        {me.capabilities.includes("people.export") && (
          <button className="button" onClick={() => download("csv")}>
            Export scoped CSV
          </button>
        )}
        {scenario.manifest_ready && (
          <button className="button" onClick={() => download("manifest")}>
            Download approved manifest
          </button>
        )}
        <label>
          Forecast revision
          <select
            aria-label="Forecast revision"
            value={revision || scenario.revision}
            onChange={(event) => setRevision(Number(event.target.value))}
          >
            {scenario.revisions.map((row) => (
              <option key={row.id} value={row.number}>
                Revision {row.number} · {row.reason}
              </option>
            ))}
          </select>
        </label>
      </div>
      {downloadError && (
        <p className="error-box" role="alert">
          {downloadError}
        </p>
      )}
      {scenario.pending_job && (
        <p className="scenario-notice" role="status">
          The forecast is saved. The independent worker is verifying pinned
          inputs and calculations; submission waits for verification.
        </p>
      )}
      {scenario.failed_job && (
        <p className="error-box" role="alert">
          A worker job exhausted its retries. The saved plan is retained.
          Restore the dependency and use Retry failed jobs with a reason.
        </p>
      )}
      {scenario.state === "REJECTED" && (
        <p className="scenario-notice" role="status">
          Returned by the reviewer. Save a new revision before resubmitting;
          prior decisions and calculations remain in history.
        </p>
      )}
      {fork && (
        <form
          className="scenario-fork"
          onSubmit={(event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            onCommand(scenario, {
              action: "branches",
              body: {
                title: String(data.get("title")),
                reviewer_id: String(data.get("reviewer")),
                reason: String(data.get("reason")),
              },
            });
          }}
        >
          <label>
            Branch title
            <input name="title" minLength={3} maxLength={100} required />
          </label>
          <label>
            Branch reviewer
            <select name="reviewer" required>
              <option value="">Select reviewer</option>
              {options.reviewers
                .filter(
                  (row) =>
                    row.scope_orgs.includes("*") ||
                    scenario.scope_orgs.every((org) =>
                      row.scope_orgs.includes(org),
                    ),
                )
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.label}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Branch reason
            <input name="reason" minLength={3} maxLength={240} required />
          </label>
          <button className="button primary" disabled={pending}>
            Create branch
          </button>
        </form>
      )}
      <div className="scenario-split">
        <aside className="scenario-assumptions">
          <h3>Pinned assumptions</h3>
          <dl>
            <dt>Historical source</dt>
            <dd>
              {options.snapshots.find(
                (row) => row.id === scenario.base_snapshot_id,
              )?.label || scenario.base_snapshot_id}
            </dd>
            <dt>Cost policy</dt>
            <dd>
              {options.policies.find((row) => row.id === scenario.policy_id)
                ?.label || scenario.policy_id}
            </dd>
            <dt>FX assumptions</dt>
            <dd>
              {options.fx_sets.find((row) => row.id === scenario.fx_set_id)
                ?.label || scenario.fx_set_id}
            </dd>
            <dt>Calendar</dt>
            <dd>
              {scenario.year} · planning as of {scenario.planning_date}
            </dd>
            <dt>Department scope</dt>
            <dd>{scenario.scope_orgs.join(", ")}</dd>
            <dt>Budget cap · minor units</dt>
            <dd>
              {scenario.budget_minor} {scenario.currency}
            </dd>
            <dt>Owner / reviewer</dt>
            <dd>
              {scenario.owner_id} / {scenario.reviewer_id}
            </dd>
          </dl>
          <details>
            <summary>Assumption documents</summary>
            <pre>
              {JSON.stringify(
                {
                  policy: options.policies.find(
                    (row) => row.id === scenario.policy_id,
                  )?.document,
                  fx: options.fx_sets.find(
                    (row) => row.id === scenario.fx_set_id,
                  )?.document,
                  rules: scenario.rules,
                },
                null,
                2,
              )}
            </pre>
          </details>
          {editable ? (
            <ScenarioOperation
              scenario={scenario}
              pending={pending}
              onSave={(op, reason) =>
                onCommand(scenario, {
                  action: "operations",
                  body: { operations: [op], reason },
                })
              }
            />
          ) : (
            <p className="fine-print">
              The operation log is locked for your current action and lifecycle
              state.
            </p>
          )}
          <h3>Current operation log</h3>
          {scenario.operations.length ? (
            <ol className="operation-log">
              {scenario.operations.map((op) => (
                <li key={op.id}>
                  <strong>{op.kind.replaceAll("_", " ")}</strong>
                  <span>
                    {op.effective_date} ·{" "}
                    {"employee_id" in op
                      ? op.employee_id
                      : "org_id" in op
                        ? op.org_id
                        : op.hire_operation_id}
                  </span>
                  <p>{op.reason}</p>
                  <details>
                    <summary>Operation inputs</summary>
                    <pre>{JSON.stringify(op, null, 2)}</pre>
                  </details>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted">
              No operations. This revision calculates the pinned source
              unchanged.
            </p>
          )}
          <form
            className="scenario-actions"
            onSubmit={(event) => {
              event.preventDefault();
              const data = new FormData(event.currentTarget);
              const action = (
                event.nativeEvent as SubmitEvent
              ).submitter?.getAttribute("data-action");
              const reason = String(data.get("reason"));
              if (action === "approve" || action === "return")
                onCommand(scenario, {
                  action: "decisions",
                  body: { decision: action, reason },
                });
              else if (
                action === "undo" ||
                action === "submit" ||
                action === "review" ||
                action === "promote" ||
                action === "retry"
              )
                onCommand(scenario, { action, body: { reason } });
            }}
          >
            <h3>Governed actions</h3>
            <label>
              Decision reason
              <textarea name="reason" minLength={3} maxLength={240} required />
            </label>
            <div>
              {editable && scenario.operations.length > 0 && (
                <button
                  className="button"
                  data-action="undo"
                  disabled={pending}
                >
                  Undo as new revision
                </button>
              )}
              {editable && scenario.state === "DRAFT" && (
                <button
                  className="button primary"
                  data-action="submit"
                  disabled={pending || !scenario.verified}
                >
                  Submit for review
                </button>
              )}
              {reviewer && scenario.state === "SUBMITTED" && (
                <button
                  className="button primary"
                  data-action="review"
                  disabled={pending}
                >
                  Start review
                </button>
              )}
              {reviewer && scenario.state === "IN_REVIEW" && (
                <>
                  <button
                    className="button"
                    data-action="return"
                    disabled={pending}
                  >
                    Return proposal
                  </button>
                  <button
                    className="button primary"
                    data-action="approve"
                    disabled={pending || !scenario.verified}
                  >
                    Approve plan
                  </button>
                </>
              )}
              {reviewer && scenario.state === "APPROVED" && (
                <button
                  className="button primary"
                  data-action="promote"
                  disabled={pending || !scenario.manifest_ready}
                >
                  Promote planning baseline
                </button>
              )}
              {scenario.failed_job && (
                <button
                  className="button"
                  data-action="retry"
                  disabled={pending}
                >
                  Retry failed jobs
                </button>
              )}
            </div>
            <small>
              Only the assigned reviewer may approve or promote. Submission and
              approval require independent worker verification.
            </small>
          </form>
        </aside>
        <div className="scenario-result">
          {forecast.isPending ? (
            <p role="status">Loading calculated months…</p>
          ) : forecast.error ? (
            <div role="alert" className="error-box">
              {forecast.error.message}
              <button className="button" onClick={() => forecast.refetch()}>
                Reload forecast
              </button>
            </div>
          ) : (
            <ScenarioForecast
              key={forecast.data.current.revision_id}
              forecast={forecast.data}
              scenario={scenario}
            />
          )}
          <section className="scenario-decisions">
            <h3>Human decisions</h3>
            {scenario.decisions.length ? (
              <ol>
                {scenario.decisions.map((row) => (
                  <li key={row.id}>
                    <strong>{row.action.replaceAll("_", " ")}</strong> ·{" "}
                    {row.actor_id}
                    <p>{row.reason}</p>
                    <small>
                      {new Date(row.created_at).toLocaleString()} ·{" "}
                      {row.revision_id}
                    </small>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted">No decisions have been submitted.</p>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}
