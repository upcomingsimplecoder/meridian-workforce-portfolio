"use client";
import { useState } from "react";
import type { CycleRequest, Options } from "@/lib/compensation";
import { parseMoneyInput } from "@/lib/compensation";

type PoolDraft = {
  manager_id: string;
  org_id: string;
  currency: "USD" | "EUR" | "GBP" | "JPY" | "KWD";
  budget: string;
  holdback: string;
};
const emptyPool = (): PoolDraft => ({
  manager_id: "",
  org_id: "",
  currency: "USD",
  budget: "",
  holdback: "0",
});
const emptyRoute = () => ({
  org_id: "",
  division_approver_id: "",
  hr_signoff_id: "",
  finance_signoff_id: "",
});
function Choices({
  name,
  values,
  value,
  onChange,
}: {
  name: string;
  values: Options["managers"];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label>
      {name}
      <select
        aria-label={name}
        required
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Select an assigned account</option>
        {values.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
            {item.scopes?.length ? ` · ${item.scopes.join(", ")}` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
export function CompensationCreate({
  options,
  pending,
  onCreate,
}: {
  options: Options;
  pending: boolean;
  onCreate: (body: CycleRequest) => void;
}) {
  const [pools, setPools] = useState([emptyPool()]);
  const [routes, setRoutes] = useState([emptyRoute()]);
  const [error, setError] = useState("");
  function updatePool(index: number, values: Partial<PoolDraft>) {
    setPools((rows) =>
      rows.map((row, i) => (i === index ? { ...row, ...values } : row)),
    );
  }
  function updateRoute(
    index: number,
    values: Partial<ReturnType<typeof emptyRoute>>,
  ) {
    setRoutes((rows) =>
      rows.map((row, i) => (i === index ? { ...row, ...values } : row)),
    );
  }
  return (
    <section className="panel comp-config">
      <h2>Configure a compensation cycle</h2>
      <p>
        Choose a published snapshot and explicit allocations. Launch freezes
        eligibility and the named review chain.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError("");
          const data = new FormData(event.currentTarget);
          try {
            const ids = String(data.get("population"))
              .split(/[\s,;]+/)
              .filter(Boolean);
            if (
              !ids.length ||
              ids.length > 5000 ||
              new Set(ids).size !== ids.length ||
              ids.some((id) => !/^SYN-\d{6}$/.test(id))
            )
              throw new Error(
                "Choose 1–5,000 distinct synthetic employee IDs from Workforce.",
              );
            onCreate({
              title: String(data.get("title")),
              snapshot_id: String(data.get("snapshot")),
              guideline_id: String(data.get("guideline")),
              fx_set_id: String(data.get("fx")),
              selected_ids: ids,
              terms: {
                eligibility_date: String(data.get("eligibility_date")),
                merit_effective_date: String(data.get("merit_effective_date")),
                bonus_service_start: String(data.get("bonus_service_start")),
                bonus_service_end: String(data.get("bonus_service_end")),
                bonus_payment_date: String(data.get("bonus_payment_date")),
              },
              pools: pools.map((pool) => {
                const exponent =
                  pool.currency === "JPY" ? 0 : pool.currency === "KWD" ? 3 : 2;
                return {
                  manager_id: pool.manager_id,
                  org_id: pool.org_id,
                  currency: pool.currency,
                  budget_minor: parseMoneyInput(pool.budget, exponent),
                  holdback_minor: parseMoneyInput(pool.holdback, exponent),
                };
              }),
              approvals: routes,
              reason: String(data.get("reason")),
            });
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Check the configuration.",
            );
          }
        }}
      >
        <fieldset disabled={pending}>
          <legend className="sr-only">Cycle configuration</legend>
          <div className="comp-form-grid">
            <label>
              Cycle title
              <input required name="title" minLength={3} maxLength={100} />
            </label>
            <label>
              Workforce snapshot
              <select
                required
                aria-label="Workforce snapshot"
                name="snapshot"
                defaultValue=""
              >
                <option value="">Select a published source</option>
                {options.snapshots.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Guideline version
              <select
                required
                aria-label="Guideline version"
                name="guideline"
                defaultValue=""
              >
                <option value="">Select frozen guidelines</option>
                {options.guidelines.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Reporting FX version
              <select
                required
                aria-label="Reporting FX version"
                name="fx"
                defaultValue=""
              >
                <option value="">Select pinned reporting FX</option>
                {options.fx_sets.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.label}
                  </option>
                ))}
              </select>
            </label>
            {[
              ["eligibility_date", "Eligibility date"],
              ["merit_effective_date", "Merit effective date"],
              ["bonus_service_start", "Bonus service start"],
              ["bonus_service_end", "Bonus service end (exclusive)"],
              ["bonus_payment_date", "Bonus payment date"],
            ].map(([name, label]) => (
              <label key={name}>
                {label}
                <input required type="date" name={name} />
              </label>
            ))}
          </div>
          <label>
            Selected synthetic employee IDs
            <textarea
              required
              name="population"
              rows={3}
              aria-describedby="population-help"
            />
          </label>
          <p id="population-help" className="fine-print">
            Copy IDs from the chosen Workforce snapshot. Eligibility exclusions
            are retained with reasons after launch.
          </p>
          <h3>Manager allocations</h3>
          <p className="fine-print">
            One allocation per eligible manager, department and currency. These
            reserve annualized merit plus the prorated one-time bonus;
            first-year merit cost is reported separately.
          </p>
          {pools.map((pool, index) => (
            <fieldset key={index} className="comp-allocation">
              <legend>Allocation {index + 1}</legend>
              <div className="comp-form-grid">
                <Choices
                  name={`Manager ${index + 1}`}
                  values={options.managers}
                  value={pool.manager_id}
                  onChange={(manager_id) => updatePool(index, { manager_id })}
                />
                <label>
                  Department {index + 1}
                  <input
                    required
                    pattern="D[0-9]{2}"
                    value={pool.org_id}
                    onChange={(event) =>
                      updatePool(index, { org_id: event.target.value })
                    }
                  />
                </label>
                <label>
                  Currency {index + 1}
                  <select
                    aria-label={`Currency ${index + 1}`}
                    value={pool.currency}
                    onChange={(event) =>
                      updatePool(index, {
                        currency: event.target.value as PoolDraft["currency"],
                      })
                    }
                  >
                    {["USD", "EUR", "GBP", "JPY", "KWD"].map((currency) => (
                      <option key={currency}>{currency}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Budget {index + 1}
                  <input
                    required
                    inputMode="decimal"
                    value={pool.budget}
                    onChange={(event) =>
                      updatePool(index, { budget: event.target.value })
                    }
                  />
                </label>
                <label>
                  Holdback {index + 1}
                  <input
                    required
                    inputMode="decimal"
                    value={pool.holdback}
                    onChange={(event) =>
                      updatePool(index, { holdback: event.target.value })
                    }
                  />
                </label>
              </div>
              {pools.length > 1 && (
                <button
                  type="button"
                  className="button"
                  onClick={() =>
                    setPools((rows) => rows.filter((_, i) => i !== index))
                  }
                >
                  Remove allocation {index + 1}
                </button>
              )}
            </fieldset>
          ))}
          <button
            className="button"
            type="button"
            disabled={pools.length >= 100}
            onClick={() => setPools((rows) => [...rows, emptyPool()])}
          >
            Add manager allocation
          </button>
          <h3>Named serial reviews</h3>
          <p className="fine-print">
            Division → HR → Finance. Three separate reviewers; each must be
            separate from every proposer in that department.
          </p>
          {routes.map((route, index) => (
            <fieldset key={index} className="comp-allocation">
              <legend>Review chain {index + 1}</legend>
              <div className="comp-form-grid">
                <label>
                  Review department {index + 1}
                  <input
                    required
                    pattern="D[0-9]{2}"
                    value={route.org_id}
                    onChange={(event) =>
                      updateRoute(index, { org_id: event.target.value })
                    }
                  />
                </label>
                <Choices
                  name={`Division reviewer ${index + 1}`}
                  values={options.division_approvers}
                  value={route.division_approver_id}
                  onChange={(division_approver_id) =>
                    updateRoute(index, { division_approver_id })
                  }
                />
                <Choices
                  name={`HR signer ${index + 1}`}
                  values={options.hr_signers}
                  value={route.hr_signoff_id}
                  onChange={(hr_signoff_id) =>
                    updateRoute(index, { hr_signoff_id })
                  }
                />
                <Choices
                  name={`Finance signer ${index + 1}`}
                  values={options.finance_signers}
                  value={route.finance_signoff_id}
                  onChange={(finance_signoff_id) =>
                    updateRoute(index, { finance_signoff_id })
                  }
                />
              </div>
              {routes.length > 1 && (
                <button
                  className="button"
                  type="button"
                  onClick={() =>
                    setRoutes((rows) => rows.filter((_, i) => i !== index))
                  }
                >
                  Remove review chain {index + 1}
                </button>
              )}
            </fieldset>
          ))}
          <button
            className="button"
            type="button"
            disabled={routes.length >= 100}
            onClick={() => setRoutes((rows) => [...rows, emptyRoute()])}
          >
            Add review chain
          </button>
          <label>
            Configuration reason
            <textarea
              name="reason"
              required
              minLength={3}
              maxLength={240}
              rows={2}
            />
          </label>
          {error && (
            <p role="alert" className="error-box">
              {error}
            </p>
          )}
          <button className="button primary" type="submit">
            {pending ? "Saving configuration…" : "Save cycle configuration"}
          </button>
        </fieldset>
      </form>
    </section>
  );
}
