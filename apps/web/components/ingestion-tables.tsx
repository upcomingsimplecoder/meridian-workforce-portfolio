"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getErrors, getPreview } from "@/lib/ingestion";
import { Failure } from "./ingestion-shared";

export function ValidationTables({
  id,
  principal,
  revision,
}: {
  id: string;
  principal: string;
  revision: number;
}) {
  const [tab, setTab] = useState<"errors" | "preview">("errors");
  return (
    <section
      className="panel validation-panel"
      aria-label="Source validation details"
    >
      <div className="validation-controls">
        <h2>Validation details</h2>
        <div className="view-switch" aria-label="Validation view">
          <button
            className="button"
            aria-pressed={tab === "errors"}
            onClick={() => setTab("errors")}
          >
            Quarantined errors
          </button>
          <button
            className="button"
            aria-pressed={tab === "preview"}
            onClick={() => setTab("preview")}
          >
            Accepted preview
          </button>
        </div>
      </div>
      {tab === "errors" ? (
        <ErrorTable key={`${id}:${revision}`} id={id} principal={principal} />
      ) : (
        <PreviewTable key={`${id}:${revision}`} id={id} principal={principal} />
      )}
    </section>
  );
}
function Pages({
  count,
  previous,
  next,
  more,
}: {
  count: number;
  previous: () => void;
  next: () => void;
  more: boolean;
}) {
  return (
    <div className="pagination">
      <span>Page {count} · Up to 20 rows</span>
      <div>
        <button className="button" disabled={count === 1} onClick={previous}>
          Previous rows
        </button>
        <button className="button" disabled={!more} onClick={next}>
          Next rows
        </button>
      </div>
    </div>
  );
}
function ErrorTable({ id, principal }: { id: string; principal: string }) {
  const [code, setCode] = useState("");
  const [cursors, setCursors] = useState([-1]);
  const errors = useQuery({
    queryKey: ["batch-errors", principal, id, code, cursors.at(-1)],
    queryFn: () => getErrors(id, cursors.at(-1) ?? -1, code),
  });
  return (
    <>
      <div className="validation-filter">
        <label>
          Error code
          <select
            value={code}
            onChange={(event) => {
              setCode(event.target.value);
              setCursors([-1]);
            }}
          >
            <option value="">All errors</option>
            {[
              "INVALID_FIELD",
              "INVALID_REPORTING_GRAPH",
              "UNKNOWN_ORG",
              "INACTIVE_ORG",
              "OVERLAPPING_VERSION",
              "AFTER_WATERMARK",
              "FORMULA_INPUT",
              "INVALID_FILE",
              "INVALID_ROW",
              "HISTORY_LIMIT",
            ].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <p>
          Row numbers refer to input records, excluding the CSV header. Row 0
          identifies a file-level error. Rejected values are redacted.
        </p>
      </div>
      {errors.isPending ? (
        <p className="source-message" role="status">
          Loading validation errors…
        </p>
      ) : errors.error ? (
        <Failure error={errors.error} retry={() => errors.refetch()} />
      ) : (
        <>
          <p className="table-caption" role="status">
            {errors.data.total} matching validation errors
          </p>
          {errors.data.items.length ? (
            <div
              className="source-table-scroll"
              tabIndex={0}
              aria-label="Quarantined error rows"
            >
              <table className="source-table">
                <thead>
                  <tr>
                    <th scope="col">Row</th>
                    <th scope="col">Field / error</th>
                    <th scope="col">Correction guidance</th>
                  </tr>
                </thead>
                <tbody>
                  {errors.data.items.map((row) => (
                    <tr key={row.row_number}>
                      <td className="numeric">{row.row_number}</td>
                      <td>
                        <strong>{row.field}</strong>
                        <code>{row.code}</code>
                      </td>
                      <td>{row.summary}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="source-message">
              <h3>
                {code ? "No errors match this filter" : "No quarantined errors"}
              </h3>
              <p>
                {code
                  ? "Choose another error code to inspect its records."
                  : "The completed validation has no rejected input rows."}
              </p>
            </div>
          )}
          <Pages
            count={cursors.length}
            previous={() => setCursors((values) => values.slice(0, -1))}
            more={errors.data.next_cursor !== null}
            next={() =>
              setCursors((values) => [...values, errors.data.next_cursor ?? -1])
            }
          />
        </>
      )}
    </>
  );
}
function PreviewTable({ id, principal }: { id: string; principal: string }) {
  const [cursors, setCursors] = useState([0]);
  const preview = useQuery({
    queryKey: ["batch-preview", principal, id, cursors.at(-1)],
    queryFn: () => getPreview(id, cursors.at(-1) ?? 0),
  });
  return (
    <>
      <p className="table-caption">
        Accepted source rows · Restricted pay fields excluded from this preview
      </p>
      {preview.isPending ? (
        <p className="source-message" role="status">
          Loading accepted rows…
        </p>
      ) : preview.error ? (
        <Failure error={preview.error} retry={() => preview.refetch()} />
      ) : (
        <>
          <p className="table-caption">
            {preview.data.total.toLocaleString("en-US")} accepted input rows
          </p>
          <div
            className="source-table-scroll"
            tabIndex={0}
            aria-label="Accepted source rows"
          >
            <table className="source-table">
              <thead>
                <tr>
                  <th scope="col">Row</th>
                  <th scope="col">Synthetic identity</th>
                  <th scope="col">Department</th>
                  <th scope="col">Manager</th>
                  <th scope="col">Effective interval</th>
                </tr>
              </thead>
              <tbody>
                {preview.data.items.map((row) => (
                  <tr key={row.row_number}>
                    <td>{row.row_number}</td>
                    <td>
                      <strong>{row.display_name}</strong>
                      <code>
                        {row.employee_id} · {row.status}
                      </code>
                    </td>
                    <td>{row.org_id}</td>
                    <td>{row.manager_id || "Root"}</td>
                    <td className="nowrap">
                      {row.valid_from}
                      <small>to {row.valid_to || "open interval"}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!preview.data.items.length && (
            <p className="source-message">No accepted rows in this source.</p>
          )}
          <Pages
            count={cursors.length}
            previous={() => setCursors((values) => values.slice(0, -1))}
            more={preview.data.next_cursor !== null}
            next={() =>
              setCursors((values) => [...values, preview.data.next_cursor ?? 0])
            }
          />
        </>
      )}
    </>
  );
}
