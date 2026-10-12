import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ScenarioOperation } from "@/components/scenario-operation";
import { ScenarioCreate } from "@/components/scenario-create";
import { money, type Scenario, type ScenarioOptions } from "@/lib/scenarios";
afterEach(cleanup);

const scenario = {
  year: 2028,
  scope_orgs: ["D01"],
  rules: { hire_lead_days: 14 },
  operations: [],
} as unknown as Scenario;
it("preserves 20-digit original money and calendar dates in the hire request", () => {
  const onSave = vi.fn();
  render(
    <ScenarioOperation scenario={scenario} pending={false} onSave={onSave} />,
  );
  fireEvent.change(screen.getByLabelText("Effective date"), {
    target: { value: "2028-02-29" },
  });
  fireEvent.change(screen.getByLabelText("Synthetic employee ID"), {
    target: { value: "SYN-900001" },
  });
  fireEvent.change(screen.getByLabelText("Synthetic position ID"), {
    target: { value: "POS-900001" },
  });
  fireEvent.change(screen.getByLabelText(/Annual pay in original/), {
    target: { value: "99999999999999999999" },
  });
  fireEvent.change(screen.getByLabelText("Original currency"), {
    target: { value: "KWD" },
  });
  fireEvent.change(screen.getByLabelText("FTE"), {
    target: { value: "0.5000" },
  });
  fireEvent.change(screen.getByLabelText("Operation reason"), {
    target: { value: "Synthetic hire rationale" },
  });
  fireEvent.submit(
    screen.getByRole("button", { name: "Save operation" }).closest("form")!,
  );
  expect(onSave).toHaveBeenCalledWith(
    expect.objectContaining({
      kind: "hire",
      effective_date: "2028-02-29",
      annual_pay_minor: "99999999999999999999",
      currency: "KWD",
      fte: "0.5000",
      manager_id: null,
    }),
    "Synthetic hire rationale",
  );
});
it("a vacancy delay references a persisted hire instead of another employee payload", () => {
  const onSave = vi.fn();
  const hire = {
    id: "6e26c079-dfaf-4160-8f53-de96204e6740",
    kind: "hire",
    employee_id: "SYN-900001",
    effective_date: "2028-02-29",
  } as Scenario["operations"][number];
  render(
    <ScenarioOperation
      scenario={{ ...scenario, operations: [hire] }}
      pending={false}
      onSave={onSave}
    />,
  );
  fireEvent.change(screen.getByLabelText("Change type"), {
    target: { value: "vacancy_delay" },
  });
  expect(
    screen.queryByLabelText("Synthetic employee ID"),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Prior hire"), {
    target: { value: hire.id },
  });
  fireEvent.change(screen.getByLabelText("Effective date"), {
    target: { value: "2028-03-31" },
  });
  fireEvent.change(screen.getByLabelText("Operation reason"), {
    target: { value: "Revised synthetic start date" },
  });
  fireEvent.submit(
    screen.getByRole("button", { name: "Save operation" }).closest("form")!,
  );
  expect(onSave).toHaveBeenCalledWith(
    expect.objectContaining({
      kind: "vacancy_delay",
      hire_operation_id: hire.id,
      effective_date: "2028-03-31",
    }),
    "Revised synthetic start date",
  );
});
it("reviewer choices respect the complete selected scope and large budgets stay strings", () => {
  const onCreate = vi.fn();
  const options: ScenarioOptions = {
    snapshots: [{ id: "snapshot", label: "Historical source" }],
    policies: [{ id: "policy", label: "Cost policy", document: {} }],
    fx_sets: [{ id: "fx", label: "Pinned FX", document: {} }],
    scope_orgs: [
      { id: "D01", label: "One" },
      { id: "D02", label: "Two" },
    ],
    reviewers: [
      { id: "one", label: "Scoped reviewer", scope_orgs: ["D01"] },
      { id: "all", label: "Broad reviewer", scope_orgs: ["*"] },
    ],
  };
  render(
    <ScenarioCreate options={options} pending={false} onCreate={onCreate} />,
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Select all departments" }),
  );
  expect(
    screen.queryByRole("option", { name: "Scoped reviewer" }),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Assigned reviewer"), {
    target: { value: "all" },
  });
  fireEvent.change(screen.getByLabelText("Plan title"), {
    target: { value: "Synthetic plan" },
  });
  fireEvent.change(screen.getByLabelText(/Budget cap in reporting/), {
    target: { value: "99999999999999999999" },
  });
  fireEvent.submit(
    screen.getByRole("button", { name: "Create pinned plan" }).closest("form")!,
  );
  expect(onCreate).toHaveBeenCalledWith(
    expect.objectContaining({
      scope_orgs: ["D01", "D02"],
      reviewer_id: "all",
      budget_minor: "99999999999999999999",
    }),
  );
});
it("formats authoritative signed deltas with exact reporting currency precision", () => {
  expect(money("-14048276", { currency: "USD", exponent: 2 })).toBe(
    "USD −140,482.76",
  );
  expect(money("99999999999999999999", { currency: "KWD", exponent: 3 })).toBe(
    "KWD 99,999,999,999,999,999.999",
  );
});
