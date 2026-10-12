"use client";
import { useMemo, useState } from "react";
import {
  flexRender,
  getCoreRowModel,
  createTable,
  type TableState,
  type Table,
  type ColumnDef,
} from "@tanstack/react-table";
import { formatMoney, type Person } from "@/lib/workforce";

export function WorkforceTable({
  people,
  showPay,
  onSelect,
}: {
  people: Person[];
  showPay: boolean;
  onSelect: (id: string) => void;
}) {
  const columns = useMemo<ColumnDef<Person>[]>(
    () => [
      {
        id: "person",
        header: "Person",
        cell: ({ row }) => (
          <button
            className="person-link"
            aria-label={`${row.original.display_name} ${row.original.id}`}
            onClick={(event) => {
              event.currentTarget.focus();
              onSelect(row.original.id);
            }}
          >
            <strong>{row.original.display_name}</strong>
            <span>{row.original.id}</span>
          </button>
        ),
      },
      {
        accessorKey: "org_name",
        header: "Department",
        cell: ({ row }) => (
          <>
            <span>{row.original.org_name}</span>
            <small>
              {row.original.org_id} · {row.original.region}
            </small>
          </>
        ),
      },
      {
        accessorKey: "job_family",
        header: "Job family",
        cell: ({ row }) => (
          <>
            <span>{row.original.job_family}</span>
            <small>
              {row.original.grade} · {row.original.position_id}
            </small>
          </>
        ),
      },
      {
        accessorKey: "fte",
        header: "FTE",
        cell: ({ row }) => <span className="numeric">{row.original.fte}</span>,
      },
      ...(showPay
        ? [
            {
              id: "pay",
              header: "Annual base pay",
              cell: ({ row }) => (
                <span className="numeric money">
                  {"annual_pay" in row.original
                    ? formatMoney(row.original.annual_pay)
                    : "Restricted"}
                </span>
              ),
            } satisfies ColumnDef<Person>,
          ]
        : []),
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ row }) => (
          <span className="work-status">
            {row.original.status === "ACTIVE" ? "Active" : row.original.status}
          </span>
        ),
      },
    ],
    [showPay, onSelect],
  );
  const [tableState, setTableState] = useState<Partial<TableState>>({});
  // Recreate the pure core model for each data/state change; no mutable hook object crosses renders.
  const table = useMemo(() => {
    const model: Table<Person> = createTable<Person>({
      data: people,
      columns,
      state: tableState,
      onStateChange: (updater) =>
        setTableState((previous) =>
          typeof updater === "function"
            ? updater({ ...model.initialState, ...previous })
            : updater,
        ),
      renderFallbackValue: null,
      getCoreRowModel: getCoreRowModel(),
      manualPagination: true,
    });
    model.setOptions((previous) => ({
      ...previous,
      state: { ...model.initialState, ...tableState },
    }));
    return model;
  }, [people, columns, tableState]);
  return (
    <div
      className="table-scroll"
      role="region"
      aria-label="Workforce results, scroll horizontally for more columns"
      tabIndex={0}
    >
      <table className="workforce-table">
        <caption className="sr-only">
          Permitted synthetic people at the selected effective date. Select a
          person to view history.
        </caption>
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => (
                <th key={header.id} scope="col">
                  {flexRender(
                    header.column.columnDef.header,
                    header.getContext(),
                  )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr key={row.original.id}>
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id}>
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
