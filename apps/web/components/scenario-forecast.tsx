"use client";
import { useState } from "react";
import Link from "next/link";
import { replayHref } from "@/lib/replay";
import { money, type Forecast, type Scenario } from "@/lib/scenarios";

export function ScenarioForecast({
  forecast,
  scenario,
}: {
  forecast: Forecast;
  scenario: Scenario;
}) {
  const [department, setDepartment] = useState("");
  const result = forecast.current.result;
  const rows = result.months.map((month) => ({
    month: month.month,
    cost: department
      ? month.departments.find((row) => row.org_id === department)!
      : month,
  }));
  const maxCost = result.months.reduce(
    (max, month) =>
      BigInt(month.total_minor) > max ? BigInt(month.total_minor) : max,
    1n,
  );
  const maxHC = Math.max(
    1,
    ...result.months.map((month) => month.closing_headcount),
  );
  // Only chart coordinates use Number; all money and deltas are supplied by the server.
  const costPoints = result.months
    .map(
      (month, i) =>
        `${32 + i * 49},${175 - Number((BigInt(month.total_minor) * 140n) / maxCost)}`,
    )
    .join(" ");
  const hcPoints = result.months
    .map(
      (month, i) =>
        `${32 + i * 49},${175 - (month.closing_headcount / maxHC) * 140}`,
    )
    .join(" ");
  return (
    <section className="scenario-forecast" aria-label="Calculated forecast">
      <div className="scenario-totals">
        <div>
          <span>Calculated annual cost</span>
          <strong data-testid="annual-cost">
            {money(result.annual.total_minor, result)}
          </strong>
          <small>
            {result.currency} · {result.exponent} decimal places
          </small>
        </div>
        <div>
          <span>Change from pinned baseline</span>
          <strong data-testid="annual-delta">
            <Link
              href={replayHref("SCENARIO", forecast.current.revision_id)}
              aria-label="Replay this 12-month cost delta"
            >
              {money(forecast.annual_delta_minor, result)}
            </Link>
          </strong>
          <small>Server calculated · 12 months</small>
        </div>
      </div>
      <div className="panel-heading">
        <h2>12-month outlook</h2>
        <span className="status-tag">
          {scenario.verified ? "WORKER VERIFIED" : "VERIFICATION PENDING"}
        </span>
      </div>
      <figure className="scenario-chart">
        <svg
          viewBox="0 0 610 215"
          role="img"
          aria-label={`Monthly total cost and closing headcount for ${scenario.year}. Exact values follow in the table.`}
        >
          {[35, 105, 175].map((y) => (
            <line key={y} x1="30" x2="585" y1={y} y2={y} stroke="#dce1e7" />
          ))}
          <polyline
            points={costPoints}
            fill="none"
            stroke="#203c62"
            strokeWidth="3"
          />
          <polyline
            points={hcPoints}
            fill="none"
            stroke="#477968"
            strokeWidth="2"
            strokeDasharray="5 4"
          />
          {result.months.map((month, i) => (
            <text
              key={month.month}
              x={32 + i * 49}
              y="204"
              textAnchor="middle"
              fontSize="11"
              fill="#526174"
            >
              {month.month.slice(5)}
            </text>
          ))}
        </svg>
        <figcaption>
          <span className="chart-cost">Total cost</span> ·{" "}
          <span className="chart-headcount">Closing headcount</span> · Each
          series scaled to its own maximum. Use the table for exact values.
        </figcaption>
      </figure>
      <label className="scenario-filter">
        Forecast department
        <select
          value={department}
          onChange={(event) => setDepartment(event.target.value)}
        >
          <option value="">All pinned departments</option>
          {scenario.scope_orgs.map((org) => (
            <option key={org}>{org}</option>
          ))}
        </select>
      </label>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="Monthly forecast table"
        onKeyDown={(event) => {
          if (
            event.target !== event.currentTarget ||
            event.altKey ||
            event.ctrlKey ||
            event.metaKey ||
            !["ArrowLeft", "ArrowRight"].includes(event.key)
          )
            return;
          event.preventDefault();
          event.currentTarget.scrollLeft +=
            event.key === "ArrowRight" ? 80 : -80;
        }}
      >
        <table className="data-table">
          <caption>
            Persisted monthly financial calculation · {result.currency}
          </caption>
          <thead>
            <tr>
              <th scope="col">Month</th>
              <th scope="col">Closing HC</th>
              <th scope="col">Average FTE</th>
              <th scope="col">Base</th>
              <th scope="col">Benefits</th>
              <th scope="col">Variable</th>
              <th scope="col">Employer tax</th>
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ month, cost }) => (
              <tr key={month}>
                <th scope="row">{month}</th>
                <td>{cost.closing_headcount.toLocaleString()}</td>
                <td>{cost.average_fte}</td>
                <td>{money(cost.base_minor, result)}</td>
                <td>{money(cost.benefits_minor, result)}</td>
                <td>{money(cost.variable_minor, result)}</td>
                <td>{money(cost.employer_tax_minor, result)}</td>
                <td>
                  <strong>{money(cost.total_minor, result)}</strong>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="scenario-explanation">
        <h3>Why the cost changed</h3>
        <dl className="component-deltas">
          {Object.entries(forecast.annual_component_delta).map(
            ([component, minor]) => (
              <div key={component}>
                <dt>{component.replace("_minor", "").replaceAll("_", " ")}</dt>
                <dd>{money(minor, result)}</dd>
              </div>
            ),
          )}
        </dl>
        <p>
          Each operation changes effective-dated assignments. Base pay is
          prorated by active calendar days and FTE, then the pinned benefits,
          variable accrual, tax and directional FX assumptions are applied.
          Monthly component rounding is half-even. The comparison is against{" "}
          {scenario.parent_scenario_id
            ? "the exact parent revision captured at branching"
            : "this plan’s original revision"}
          .
        </p>
        <p>
          {forecast.current.revision_id === scenario.revisions.at(-1)?.id
            ? `${scenario.operations.length} persisted operations`
            : "Historical revision"}{" "}
          · {result.engine_version} · {forecast.current.operation_model_version}
        </p>
      </div>
      <details className="lineage">
        <summary>Calculation inputs and provenance</summary>
        <dl>
          <dt>Snapshot</dt>
          <dd>{result.inputs.snapshot_id}</dd>
          <dt>Snapshot SHA-256</dt>
          <dd>{result.inputs.snapshot_sha256}</dd>
          <dt>Source batch</dt>
          <dd>{forecast.current.source_batch_id}</dd>
          <dt>Cost policy</dt>
          <dd>{result.inputs.policy_id}</dd>
          <dt>Policy SHA-256</dt>
          <dd>{result.inputs.policy_sha256}</dd>
          <dt>FX version</dt>
          <dd>{result.inputs.fx_set_id}</dd>
          <dt>FX SHA-256</dt>
          <dd>{result.inputs.fx_sha256}</dd>
          <dt>Organization calendar SHA-256</dt>
          <dd>{forecast.current.org_calendar_sha256}</dd>
          <dt>Forecast SHA-256</dt>
          <dd>{forecast.manifest_sha256}</dd>
        </dl>
      </details>
    </section>
  );
}
