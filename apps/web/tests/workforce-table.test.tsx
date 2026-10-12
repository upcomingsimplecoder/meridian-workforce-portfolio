import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { WorkforceTable } from "@/components/workforce-table";
import type { Person } from "@/lib/workforce";
afterEach(cleanup);
const person: Person = {
  id: "SYN-000001",
  version_id: "synthetic-version",
  display_name: "Synthetic Unit Fixture",
  org_id: "D01",
  org_name: "Synthetic Department",
  manager_id: null,
  position_id: "POS-000001",
  job_family: "Research",
  grade: "L1",
  region: "Americas",
  status: "ACTIVE",
  fte: "1.0000",
  valid_from: "2027-01-01",
  valid_to: null,
  recorded_at: "2027-01-01T00:00:00Z",
  source_batch_id: "synthetic-batch",
  annual_pay: { currency: "USD", minor_units: "123456", exponent: 2 },
};
it("renders a fresh table model when the server page changes and preserves person actions", () => {
  const selected = vi.fn();
  const { rerender } = render(
    <WorkforceTable people={[person]} showPay onSelect={selected} />,
  );
  expect(
    screen.getByRole("columnheader", { name: "Annual base pay" }),
  ).toBeVisible();
  expect(screen.getByText("USD 1,234.56")).toBeVisible();
  fireEvent.click(
    screen.getByRole("button", { name: "Synthetic Unit Fixture SYN-000001" }),
  );
  expect(selected).toHaveBeenCalledWith("SYN-000001");
  rerender(
    <WorkforceTable
      people={[
        {
          ...person,
          id: "SYN-000002",
          display_name: "Second Synthetic Fixture",
        },
      ]}
      showPay
      onSelect={selected}
    />,
  );
  expect(screen.queryByText("Synthetic Unit Fixture")).not.toBeInTheDocument();
  expect(screen.getByText("Second Synthetic Fixture")).toBeVisible();
});
it("removes restricted pay columns from the rendered table", () => {
  render(
    <WorkforceTable people={[person]} showPay={false} onSelect={vi.fn()} />,
  );
  expect(
    screen.queryByRole("columnheader", { name: "Annual base pay" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText("USD 1,234.56")).not.toBeInTheDocument();
});
