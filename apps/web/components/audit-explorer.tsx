"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import { getAudit, type AuditFilters } from "@/lib/replay";
import { replayHorizontal } from "./replay-detail";

export function AuditExplorer({ me }: { me: Me }) {
  const [correlation, setCorrelation] = useState("");
  const [permission, setPermission] = useState<"" | "ALLOW" | "DENY">("");
  const [verb, setVerb] = useState("");
  const [filters, setFilters] = useState<AuditFilters>({});
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const query = useQuery({
    queryKey: ["audit", me.id, filters, cursors.at(-1)],
    queryFn: () => getAudit(filters, cursors.at(-1)),
    gcTime: 0,
  });
  return (
    <section className="panel audit-explorer">
      <div className="panel-heading">
        <h2>Audit Explorer</h2>
        <button className="button" onClick={() => query.refetch()}>
          Refresh audit events
        </button>
      </div>
      <p>
        Append-only business and denied-action events. Entity and actor
        references are fingerprints; personnel fields and request bodies are
        withheld.
      </p>
      <form
        className="replay-fields"
        onSubmit={(event) => {
          event.preventDefault();
          setFilters({
            correlation_id: correlation.trim() || undefined,
            permission: permission || undefined,
            verb: verb.trim() || undefined,
          });
          setCursors([undefined]);
        }}
      >
        <label>
          Correlation ID
          <input
            aria-label="Correlation ID"
            value={correlation}
            onChange={(event) => setCorrelation(event.target.value)}
            pattern="[0-9a-fA-F-]{36}"
            maxLength={36}
          />
        </label>
        <label>
          Permission result
          <select
            aria-label="Permission result"
            value={permission}
            onChange={(event) =>
              setPermission(event.target.value as typeof permission)
            }
          >
            <option value="">All results</option>
            <option value="ALLOW">Allowed</option>
            <option value="DENY">Denied</option>
          </select>
        </label>
        <label>
          Exact event verb
          <input
            aria-label="Exact event verb"
            value={verb}
            pattern="[a-z0-9_.]+"
            maxLength={100}
            onChange={(event) => setVerb(event.target.value)}
          />
        </label>
        <button className="button">Apply audit filters</button>
      </form>
      {query.isPending ? (
        <p role="status">Loading bounded audit events…</p>
      ) : query.error ? (
        <p role="alert" className="error-box">
          {query.error.message}
        </p>
      ) : (
        <>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Audit event table"
            onKeyDown={replayHorizontal}
          >
            <table className="data-table">
              <caption>Persisted audit events · 25 per page</caption>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Verb</th>
                  <th scope="col">Permission</th>
                  <th scope="col">Entity</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Policy</th>
                  <th scope="col">Correlation / event SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((row) => (
                  <tr key={row.id}>
                    <td>{new Date(row.occurred_at).toLocaleString()}</td>
                    <th scope="row">{row.verb}</th>
                    <td>{row.permission}</td>
                    <td>
                      {row.entity_type} · {row.entity_reference}
                    </td>
                    <td>{row.actor_reference || "System"}</td>
                    <td>{row.policy_version}</td>
                    <td>
                      <code className="replay-hash">{row.request_id}</code>
                      <code className="replay-hash">{row.sha256}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {query.data.items.length === 0 && (
            <p>No audit events match these filters.</p>
          )}
        </>
      )}
      <div className="pagination">
        <button
          className="button"
          disabled={cursors.length < 2}
          onClick={() => setCursors((value) => value.slice(0, -1))}
        >
          Previous audit page
        </button>
        <span>Page {cursors.length}</span>
        <button
          className="button"
          disabled={!query.data?.next_cursor}
          onClick={() =>
            setCursors((value) => [
              ...value,
              query.data?.next_cursor || undefined,
            ])
          }
        >
          Next audit page
        </button>
      </div>
    </section>
  );
}
