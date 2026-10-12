"use client";
import { useState } from "react";
import type { Operation, Scenario } from "@/lib/scenarios";

const labels: Record<Operation["kind"], string> = {
  hire: "Hire",
  departure: "Departure",
  transfer: "Transfer",
  promotion: "Promotion",
  merit_assumption: "Merit assumption",
  benefit_multiplier: "Benefits multiplier",
  vacancy_delay: "Vacancy delay",
};

export function ScenarioOperation({
  scenario,
  pending,
  onSave,
}: {
  scenario: Scenario;
  pending: boolean;
  onSave: (op: Operation, reason: string) => void;
}) {
  const [kind, setKind] = useState<Operation["kind"]>("hire");
  return (
    <form
      className="operation-form"
      onSubmit={(event) => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        const text = (name: string) => String(data.get(name) || "");
        const base = {
          id: crypto.randomUUID(),
          effective_date: text("effective"),
          reason: text("reason"),
        };
        let op: Operation;
        switch (kind) {
          case "hire":
            op = {
              ...base,
              kind,
              employee_id: text("employee"),
              position_id: text("position"),
              org_id: text("org"),
              manager_id: text("manager") || null,
              grade: text("grade"),
              annual_pay_minor: text("pay"),
              currency: text("currency") as
                | "USD"
                | "EUR"
                | "GBP"
                | "JPY"
                | "KWD",
              fte: text("fte"),
            };
            break;
          case "departure":
            op = { ...base, kind, employee_id: text("employee") };
            break;
          case "transfer":
            op = {
              ...base,
              kind,
              employee_id: text("employee"),
              org_id: text("org"),
              manager_id: text("manager") || null,
            };
            break;
          case "promotion":
            op = {
              ...base,
              kind,
              employee_id: text("employee"),
              grade: text("grade"),
              annual_pay_minor: text("pay"),
            };
            break;
          case "merit_assumption":
            op = { ...base, kind, org_id: text("org"), rate: text("rate") };
            break;
          case "benefit_multiplier":
            op = {
              ...base,
              kind,
              org_id: text("org"),
              multiplier: text("multiplier"),
            };
            break;
          case "vacancy_delay":
            op = { ...base, kind, hire_operation_id: text("hire") };
            break;
        }
        onSave(op, text("reason"));
      }}
    >
      <h3>Add an operation</h3>
      <label>
        Change type
        <select
          value={kind}
          onChange={(event) => setKind(event.target.value as Operation["kind"])}
        >
          {Object.entries(labels).map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Effective date
        <input
          name="effective"
          type="date"
          min={`${scenario.year}-01-01`}
          max={`${scenario.year}-12-31`}
          required
        />
      </label>
      {!["merit_assumption", "benefit_multiplier", "vacancy_delay"].includes(
        kind,
      ) && (
        <label>
          Synthetic employee ID
          <input
            name="employee"
            pattern="SYN-[0-9]{6}"
            placeholder="SYN-900001"
            required
          />
        </label>
      )}
      {kind === "hire" && (
        <label>
          Synthetic position ID
          <input
            name="position"
            pattern="POS-[0-9]{6}"
            placeholder="POS-900001"
            required
          />
        </label>
      )}
      {["hire", "transfer", "merit_assumption", "benefit_multiplier"].includes(
        kind,
      ) && (
        <label>
          Operation department
          <select name="org">
            {scenario.scope_orgs.map((org) => (
              <option key={org}>{org}</option>
            ))}
          </select>
        </label>
      )}
      {["hire", "transfer"].includes(kind) && (
        <label>
          Manager ID (optional)
          <input
            name="manager"
            pattern="SYN-[0-9]{6}"
            placeholder="SYN-000001"
          />
          <small>Leave empty for a root position.</small>
        </label>
      )}
      {["hire", "promotion"].includes(kind) && (
        <>
          <label>
            Grade
            <select name="grade">
              {Array.from({ length: 10 }, (_, i) => (
                <option key={i}>L{i + 1}</option>
              ))}
            </select>
          </label>
          <label>
            Annual pay in original minor units
            <input
              name="pay"
              inputMode="numeric"
              pattern="[0-9]{1,20}"
              required
            />
            <small>USD/EUR/GBP: cents · JPY: yen · KWD: fils.</small>
          </label>
        </>
      )}
      {kind === "hire" && (
        <>
          <label>
            Original currency
            <select name="currency">
              {["USD", "EUR", "GBP", "JPY", "KWD"].map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          <label>
            FTE
            <input
              name="fte"
              inputMode="decimal"
              defaultValue="1.0000"
              pattern="[0-9](\.[0-9]{1,4})?"
              required
            />
          </label>
        </>
      )}
      {kind === "merit_assumption" && (
        <label>
          Merit rate
          <input
            name="rate"
            inputMode="decimal"
            placeholder="0.03"
            pattern="0(\.[0-9]{1,6})?"
            required
          />
          <small>0.03 represents 3%; maximum 0.25.</small>
        </label>
      )}
      {kind === "benefit_multiplier" && (
        <label>
          Benefits multiplier
          <input
            name="multiplier"
            inputMode="decimal"
            placeholder="1.25"
            pattern="[1-3](\.[0-9]{1,6})?"
            required
          />
          <small>1.25 adds benefits equal to 25% of base pay.</small>
        </label>
      )}
      {kind === "vacancy_delay" && (
        <label>
          Prior hire
          <select name="hire" required>
            <option value="">Select a hire operation</option>
            {scenario.operations
              .filter((op) => op.kind === "hire")
              .map((op) => (
                <option key={op.id} value={op.id}>
                  {op.employee_id} · {op.effective_date}
                </option>
              ))}
          </select>
        </label>
      )}
      <label>
        Operation reason
        <textarea name="reason" minLength={3} maxLength={240} required />
      </label>
      <p className="fine-print">
        Changes follow date order. Hires require{" "}
        {String(scenario.rules.hire_lead_days)} days of lead time. The server
        validates scope, reporting relationships and the pinned budget.
      </p>
      <button className="button primary" disabled={pending}>
        {pending ? "Calculating and saving…" : "Save operation"}
      </button>
    </form>
  );
}
