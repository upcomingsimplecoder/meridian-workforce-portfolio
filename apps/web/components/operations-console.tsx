"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, type Me } from "@/lib/api";
import { getOperations, getTrace } from "@/lib/operations";

export function OperationsConsole({ me }: { me: Me }) {
  const permitted = me.capabilities.includes("operations.read");
  const [traceId, setTraceId] = useState("");
  const metrics = useQuery({
    queryKey: ["operations", me.id],
    queryFn: getOperations,
    enabled: permitted,
    refetchInterval: 5000,
  });
  const trace = useQuery({
    queryKey: ["operations-trace", me.id, traceId],
    queryFn: () => getTrace(traceId),
    enabled: permitted && !!traceId,
  });
  const data = metrics.data;
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">SERVICE HEALTH & CORRELATION</div>
          <h1>Operations</h1>
          <p>Observed requests, durable jobs and analytics freshness.</p>
        </div>
        {permitted && (
          <button
            className="button"
            onClick={() => metrics.refetch()}
            disabled={metrics.isFetching}
          >
            Refresh observations
          </button>
        )}
      </div>
      {!permitted ? (
        <section className="panel empty-state">
          <h2>Operational access is restricted</h2>
          <p>Your current grants do not permit technical telemetry.</p>
        </section>
      ) : metrics.isPending ? (
        <p role="status">Loading persisted observations…</p>
      ) : metrics.error ? (
        <div className="error-box" role="alert">
          {metrics.error instanceof ApiError
            ? metrics.error.message
            : "Observations are unavailable."}
        </div>
      ) : (
        data && (
          <>
            <p className="fine-print">
              15-minute window · measured{" "}
              {new Date(data.measured_at).toLocaleString()} ·{" "}
              {data.sample_truncated
                ? `Latest ${data.sample_limit} spans; window is truncated.`
                : "Stored sample is not truncated."}{" "}
              Requests still in progress appear after completion. This is local
              recorded telemetry, not a production SLO.
            </p>
            <div className="metric-grid">
              <Metric
                label="Observed HTTP requests"
                value={String(data.http_requests)}
              />
              <Metric
                label="HTTP p95"
                value={
                  data.http_p95_ms === null
                    ? "Unobserved"
                    : `${Number(data.http_p95_ms).toLocaleString(undefined, { maximumFractionDigits: 1 })} ms`
                }
              />
              <Metric label="Server errors" value={String(data.http_errors)} />
              <Metric label="Pending jobs" value={String(data.pending_jobs)} />
            </div>
            <section className="panel">
              <h2>Alerts & freshness</h2>
              {data.alerts.length ? (
                <ul>
                  {data.alerts.map((a) => (
                    <li key={a.code}>
                      <strong>{a.code.replaceAll("_", " ")}</strong>: {a.detail}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No configured alert threshold is currently breached.</p>
              )}
              <p>
                Dead jobs: {data.dead_jobs}. Oldest pending event:{" "}
                {data.oldest_pending_seconds === null
                  ? "none"
                  : `${data.oldest_pending_seconds} seconds`}
                .
              </p>
              <p>
                Last analytical publication:{" "}
                {data.last_analytics_publication
                  ? new Date(data.last_analytics_publication).toLocaleString()
                  : "Unobserved"}
                . Latest published source matches:{" "}
                {data.analytics_matches_latest_snapshot ? "Yes" : "No"}.
              </p>
              <ul>
                {data.workers.map((w) => (
                  <li key={w.name}>
                    {w.name} worker · {w.state} ·{" "}
                    {w.age_seconds === null
                      ? "no heartbeat"
                      : `${w.age_seconds} seconds since heartbeat`}
                  </li>
                ))}
              </ul>
            </section>
            <section className="panel" aria-label="Recent completed spans">
              <h2>Recent completed spans</h2>
              <p>
                Technical identifiers and route templates. Command inputs,
                headers, narratives and personnel values are excluded.
              </p>
              <div
                className="table-scroll operations-spans"
                tabIndex={0}
                role="region"
                aria-label="Span table"
              >
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Service</th>
                      <th>Route / event</th>
                      <th>Outcome</th>
                      <th>Duration</th>
                      <th>Trace</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_spans.map((s) => (
                      <tr key={s.span_id}>
                        <td>{s.service}</td>
                        <td>{s.route || s.event_type || "Unclassified"}</td>
                        <td>{s.http_status || s.outcome}</td>
                        <td>{s.duration_ms} ms</td>
                        <td>
                          <button
                            className="button"
                            aria-label={`Inspect trace ${s.trace_id}`}
                            onClick={() => setTraceId(s.trace_id)}
                          >
                            {s.trace_id.slice(0, 12)}…
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!data.recent_spans.length && (
                <p role="status">
                  No completed spans have been recorded in this window.
                </p>
              )}
            </section>
            {traceId && (
              <section className="panel" aria-label="Trace details">
                <h2>Trace correlation</h2>
                <p className="mono">{traceId}</p>
                {trace.isPending ? (
                  <p role="status">Loading trace…</p>
                ) : trace.error ? (
                  <div role="alert" className="error-box">
                    Trace could not be loaded.
                  </div>
                ) : (
                  trace.data && (
                    <>
                      <p>
                        {trace.data.truncated
                          ? `First ${trace.data.span_limit} spans; additional spans omitted.`
                          : `${trace.data.spans.length} recorded spans.`}
                      </p>
                      <ol>
                        {trace.data.spans.map((s) => (
                          <li key={s.span_id}>
                            <strong>
                              {s.service} · {s.kind} · {s.outcome}
                            </strong>
                            <p>
                              {s.route || s.event_type} · {s.duration_ms} ms
                            </p>
                            <p className="fine-print">
                              Span {s.span_id} · parent{" "}
                              {s.parent_span_id || "root"} · request{" "}
                              {s.request_id}
                            </p>
                          </li>
                        ))}
                      </ol>
                    </>
                  )
                )}
              </section>
            )}
          </>
        )
      )}
    </>
  );
}
function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <div className="metric-top">{label}</div>
      <strong>{value}</strong>
    </div>
  );
}
