"use client";
import { useState } from "react";
import type { ScenarioOptions, ScenarioRequest } from "@/lib/scenarios";

export function ScenarioCreate({
  options,
  pending,
  onCreate,
}: {
  options: ScenarioOptions;
  pending: boolean;
  onCreate: (body: ScenarioRequest) => void;
}) {
  const [scope, setScope] = useState<string[]>(
    [options.scope_orgs[0]?.id].filter(Boolean),
  );
  return (
    <section className="panel scenario-create">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">PIN THE INPUTS</span>
          <h2>Create a plan</h2>
        </div>
      </div>
      <p className="muted">
        Choose the historical source and synthetic assumptions. They remain
        fixed through every revision and branch.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          onCreate({
            title: String(data.get("title")),
            base_snapshot_id: String(data.get("snapshot")),
            policy_id: String(data.get("policy")),
            fx_set_id: String(data.get("fx")),
            reviewer_id: String(data.get("reviewer")),
            year: Number(data.get("year")),
            planning_date: String(data.get("planning")),
            scope_orgs: scope,
            budget_minor: String(data.get("budget")),
          });
        }}
      >
        <div className="scenario-fields">
          <label>
            Plan title
            <input
              name="title"
              required
              minLength={3}
              maxLength={100}
              placeholder="2028 workforce funding"
            />
          </label>
          <label>
            Base snapshot
            <select name="snapshot" aria-label="Base snapshot" required>
              {options.snapshots.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Cost policy
            <select name="policy" required>
              {options.policies.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            FX version
            <select name="fx" required>
              {options.fx_sets.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Assigned reviewer
            <select name="reviewer" required>
              <option value="">Select a separate reviewer</option>
              {options.reviewers
                .filter(
                  (row) =>
                    row.scope_orgs.includes("*") ||
                    scope.every((org) => row.scope_orgs.includes(org)),
                )
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.label}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Forecast year
            <input
              name="year"
              type="number"
              min={1900}
              max={2199}
              defaultValue={2028}
              required
            />
          </label>
          <label>
            Planning date
            <input
              name="planning"
              type="date"
              defaultValue="2027-12-01"
              required
            />
          </label>
          <label>
            Budget cap in reporting minor units
            <input
              name="budget"
              inputMode="numeric"
              pattern="[0-9]{1,20}"
              required
              placeholder="Integer cents for USD"
            />
            <small>
              USD cents · financial calculations use the selected policy and FX.
            </small>
          </label>
        </div>
        <fieldset className="scope-picker">
          <legend>Departments</legend>
          <button
            type="button"
            className="text-button"
            onClick={() => setScope(options.scope_orgs.map((row) => row.id))}
          >
            Select all departments
          </button>
          <div>
            {options.scope_orgs.map((row) => (
              <label key={row.id}>
                <input
                  type="checkbox"
                  checked={scope.includes(row.id)}
                  onChange={(event) =>
                    setScope((prev) =>
                      event.target.checked
                        ? [...prev, row.id]
                        : prev.filter((id) => id !== row.id),
                    )
                  }
                />
                {row.id} · {row.label}
              </label>
            ))}
          </div>
        </fieldset>
        <button
          className="button primary"
          disabled={pending || scope.length === 0}
        >
          {pending ? "Calculating and saving…" : "Create pinned plan"}
        </button>
      </form>
    </section>
  );
}
