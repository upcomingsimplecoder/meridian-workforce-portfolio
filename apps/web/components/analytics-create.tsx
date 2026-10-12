"use client";
import { useState } from "react";
import type { AnalyticalOptions, AnalyticalRequest } from "@/lib/analytics";

export function AnalyticsCreateForm({
  options,
  busy,
  submit,
}: {
  options: AnalyticalOptions;
  busy: boolean;
  submit: (body: AnalyticalRequest) => Promise<unknown>;
}) {
  const [source, setSource] = useState("");
  const [asOf, setAsOf] = useState("");
  const [cycles, setCycles] = useState<string[]>([]);
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  function toggle(values: string[], id: string, selected: boolean) {
    return selected ? [...values, id] : values.filter((value) => value !== id);
  }
  return (
    <form
      className="panel analytical-create"
      onSubmit={async (event) => {
        event.preventDefault();
        setError("");
        if (reason.trim().length < 3) {
          setError("Explain the purpose of this versioned analytical run.");
          return;
        }
        try {
          await submit({
            snapshot_id: source,
            as_of: asOf,
            cycle_ids: cycles,
            scenario_revision_ids: scenarios,
            reason: reason.trim(),
          });
        } catch (failure) {
          setError(
            failure instanceof Error
              ? failure.message
              : "The run could not be acknowledged. Your selection is retained.",
          );
        }
      }}
    >
      <h2>Pin an analytical run</h2>
      <p>
        Choose immutable versions. A separate worker exports private Parquet,
        builds SQL models and reconciles every result before publication.
      </p>
      <fieldset disabled={busy}>
        <label>
          Published source
          <select
            aria-label="Published source"
            required
            value={source}
            onChange={(event) => {
              const id = event.target.value;
              setSource(id);
              setAsOf(
                options.snapshots.find((row) => row.id === id)?.as_of || "",
              );
              setCycles([]);
              setScenarios([]);
            }}
          >
            <option value="">Choose a published snapshot</option>
            {options.snapshots.map((row) => (
              <option key={row.id} value={row.id}>
                {row.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Business as-of date
          <input
            aria-label="Business as-of date"
            type="date"
            required
            min="1900-01-01"
            max="2199-12-31"
            value={asOf}
            onChange={(event) => setAsOf(event.target.value)}
          />
        </label>
        <fieldset className="analytical-choices">
          <legend>Locked compensation versions</legend>
          {options.cycles
            .filter((row) => row.snapshot_id === source)
            .map((row) => (
              <label key={row.id}>
                <input
                  type="checkbox"
                  aria-label={`Include cycle ${row.label}`}
                  checked={cycles.includes(row.id)}
                  disabled={!cycles.includes(row.id) && cycles.length >= 10}
                  onChange={(event) =>
                    setCycles(toggle(cycles, row.id, event.target.checked))
                  }
                />
                {row.label}
              </label>
            ))}
          {!options.cycles.some((row) => row.snapshot_id === source) && (
            <p>No locked, verified compensation export for this source.</p>
          )}
        </fieldset>
        <fieldset className="analytical-choices">
          <legend>Verified scenario revisions</legend>
          {options.scenario_revisions
            .filter((row) => row.snapshot_id === source)
            .map((row) => (
              <label key={row.id}>
                <input
                  type="checkbox"
                  aria-label={`Include scenario ${row.label}`}
                  checked={scenarios.includes(row.id)}
                  disabled={
                    !scenarios.includes(row.id) && scenarios.length >= 10
                  }
                  onChange={(event) =>
                    setScenarios(
                      toggle(scenarios, row.id, event.target.checked),
                    )
                  }
                />
                {row.label}
              </label>
            ))}
          {!options.scenario_revisions.some(
            (row) => row.snapshot_id === source,
          ) && (
            <p>
              No independently verified scenario revision in your current object
              scope.
            </p>
          )}
        </fieldset>
        <label>
          Analytical purpose
          <textarea
            aria-label="Analytical purpose"
            required
            minLength={3}
            maxLength={240}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button
          className="button primary"
          disabled={!source || !asOf || busy}
          type="submit"
        >
          {busy ? "Awaiting server acknowledgement…" : "Request reconciled run"}
        </button>
      </fieldset>
      {error && (
        <p role="alert" className="error-box">
          {error}
        </p>
      )}
    </form>
  );
}
