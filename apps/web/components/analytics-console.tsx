"use client";
import { useState, type KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  analyticalCommand,
  downloadAnalyticalManifest,
  getAnalyticalEvidence,
  getAnalyticalOptions,
  getAnalyticalPublic,
  getAnalyticalRun,
  getAnalyticalRuns,
  requestAnalyticalRun,
  type AnalyticalCommand,
  type AnalyticalRequest,
  type AnalyticalRun,
} from "@/lib/analytics";
import { AnalyticsCreateForm } from "./analytics-create";
import { AnalyticsRunResult } from "./analytics-result";
function scrollRunHistory(event: KeyboardEvent<HTMLElement>) {
  const region = event.currentTarget;
  if (
    event.target !== region ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    region.scrollHeight <= region.clientHeight
  )
    return;
  const page = Math.max(40, Math.floor(region.clientHeight * 0.9));
  const positions: Record<string, number> = {
    ArrowDown: region.scrollTop + 40,
    ArrowUp: region.scrollTop - 40,
    PageDown: region.scrollTop + page,
    PageUp: region.scrollTop - page,
    Home: 0,
    End: region.scrollHeight - region.clientHeight,
  };
  if (!Object.hasOwn(positions, event.key)) return;
  const position = positions[event.key];
  event.preventDefault();
  region.scrollTop = position;
}
function ErrorBox({
  error,
  refresh,
}: {
  error: Error;
  refresh: () => unknown;
}) {
  return (
    <p role="alert" className="error-box">
      {error.message}
      <button className="button" onClick={refresh}>
        Refresh current access
      </button>
    </p>
  );
}
function ExecutiveAnalytics({ me }: { me: Me }) {
  const query = useQuery({
    queryKey: ["analytics", me.id, "public"],
    queryFn: getAnalyticalPublic,
    refetchInterval: 10000,
    gcTime: 0,
  });
  return (
    <section className="panel">
      <h2>Governed workforce totals</h2>
      <p>
        Fixed global aggregates · minimum cohort five · no employee-level
        access.
      </p>
      {query.isPending ? (
        <p role="status">Loading published totals…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} refresh={() => query.refetch()} />
      ) : (
        <>
          <p role="status">{query.data.pipeline_status}</p>
          <dl className="analytical-metadata">
            <dt>Headcount</dt>
            <dd>
              {query.data.headcount?.toLocaleString() ??
                (query.data.suppressed ? "Suppressed" : "No publication")}
            </dd>
            <dt>FTE</dt>
            <dd>{query.data.fte ?? "Unavailable"}</dd>
            <dt>Business as-of</dt>
            <dd>{query.data.data_as_of || "Unavailable"}</dd>
            <dt>Source watermark</dt>
            <dd>{query.data.source_watermark || "Unavailable"}</dd>
            <dt>Last successful sync</dt>
            <dd>{query.data.last_successful_sync || "No successful sync"}</dd>
            <dt>Model</dt>
            <dd>{query.data.model_version || "Unavailable"}</dd>
          </dl>
          {query.data.pipeline_status !== "CURRENT" && (
            <p className="analytical-notice">
              These are the last reconciled totals. The source or analytical
              pipeline has changed; refresh after the next successful
              publication.
            </p>
          )}
        </>
      )}
    </section>
  );
}
function AuditAnalytics({ me }: { me: Me }) {
  const query = useQuery({
    queryKey: ["analytics", me.id, "evidence"],
    queryFn: getAnalyticalEvidence,
    gcTime: 0,
  });
  return (
    <section className="panel">
      <h2>Redacted analytical evidence</h2>
      <p>
        Run status, version counts and fingerprints. Personnel, compensation
        values and narratives remain restricted.
      </p>
      {query.isPending ? (
        <p role="status">Loading authorized evidence…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} refresh={() => query.refetch()} />
      ) : query.data.length ? (
        query.data.map((row) => (
          <article
            className="analytical-evidence"
            key={row.id}
            data-run-id={row.id}
          >
            <h3>
              {row.status} · {row.comparison_count - row.failed_comparisons}/
              {row.comparison_count} comparisons
            </h3>
            <dl className="analytical-metadata">
              <dt>Run</dt>
              <dd>{row.id}</dd>
              <dt>Source</dt>
              <dd>{row.snapshot_id}</dd>
              <dt>Business as-of</dt>
              <dd>{row.data_as_of}</dd>
              <dt>Source intervals</dt>
              <dd>{row.source_versions ?? "Pending"}</dd>
              <dt>Model</dt>
              <dd>{row.model_version}</dd>
              <dt>Input SHA-256</dt>
              <dd>{row.inputs_sha256}</dd>
              <dt>Result SHA-256</dt>
              <dd>{row.result_sha256 || "Pending"}</dd>
            </dl>
          </article>
        ))
      ) : (
        <p>No analytical run evidence exists yet.</p>
      )}
    </section>
  );
}
function FullAnalytics({ me }: { me: Me }) {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [sourceSearch, setSourceSearch] = useState("");
  const [selected, setSelected] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const prefix = ["analytics", me.id];
  const options = useQuery({
    queryKey: [...prefix, "options", sourceSearch],
    queryFn: () => getAnalyticalOptions(sourceSearch),
    enabled: showCreate,
    gcTime: 0,
  });
  const list = useQuery({
    queryKey: [...prefix, "list", cursors.at(-1)],
    queryFn: () => getAnalyticalRuns(cursors.at(-1)),
    refetchInterval: (query) =>
      query.state.data?.items.some((row) => row.status === "QUEUED")
        ? 2000
        : 10000,
    gcTime: 0,
  });
  const detail = useQuery({
    queryKey: [...prefix, "run", selected],
    queryFn: () => getAnalyticalRun(selected),
    enabled: !!selected,
    refetchInterval: (query) =>
      query.state.data?.status === "QUEUED" ? 2000 : 10000,
    gcTime: 0,
  });
  async function acknowledged(run: AnalyticalRun) {
    client.setQueryData([...prefix, "run", run.id], run);
    setSelected(run.id);
    setShowCreate(false);
    setCursors([undefined]);
    await client.invalidateQueries({ queryKey: [...prefix, "list"] });
  }
  const create = useMutation({
    mutationFn: (body: AnalyticalRequest) =>
      requestAnalyticalRun(me, body, crypto.randomUUID()),
    onSuccess: acknowledged,
  });
  const operation = useMutation({
    mutationFn: (command: AnalyticalCommand) => {
      if (!detail.data)
        throw new Error("Refresh this analytical run before acting.");
      return analyticalCommand(me, detail.data, command, crypto.randomUUID());
    },
    onSuccess: acknowledged,
  });
  const busy = create.isPending || operation.isPending;
  return (
    <>
      <div className="analytical-toolbar">
        <button
          className="button primary"
          disabled={busy}
          onClick={() => setShowCreate((value) => !value)}
        >
          {showCreate ? "Close run configuration" : "New analytical run"}
        </button>
        <button
          className="button"
          disabled={busy}
          onClick={() => {
            void list.refetch();
            if (selected) void detail.refetch();
          }}
        >
          Refresh persisted state
        </button>
      </div>
      {showCreate && (
        <>
          <form
            className="analytical-search"
            onSubmit={(event) => {
              event.preventDefault();
              setSourceSearch(search.trim());
            }}
          >
            <label>
              Search published sources
              <input
                aria-label="Search published sources"
                maxLength={100}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <button className="button" disabled={busy}>
              Find source
            </button>
          </form>
          {options.isPending ? (
            <p role="status">Loading published versions…</p>
          ) : options.error ? (
            <ErrorBox error={options.error} refresh={() => options.refetch()} />
          ) : (
            <AnalyticsCreateForm
              key={sourceSearch}
              options={options.data}
              busy={busy}
              submit={(body) => create.mutateAsync(body)}
            />
          )}
        </>
      )}
      <div className="analytical-workspace">
        <section
          className="panel analytical-runs"
          aria-label="Analytical run history"
          aria-busy={busy || list.isFetching}
          tabIndex={0}
          onKeyDown={scrollRunHistory}
        >
          <h2>Versioned runs</h2>
          {list.isPending ? (
            <p role="status">Loading authorized runs…</p>
          ) : list.error ? (
            <ErrorBox error={list.error} refresh={() => list.refetch()} />
          ) : (
            <>
              {list.data.items.length ? (
                list.data.items.map((listed) => {
                  const row =
                    detail.data?.id === listed.id ? detail.data : listed;
                  return (
                    <button
                      className="analytical-run-link"
                      key={row.id}
                      disabled={busy || list.isFetching}
                      aria-pressed={selected === row.id}
                      data-run-id={row.id}
                      onClick={() => setSelected(row.id)}
                    >
                      <strong>{row.snapshot_title}</strong>
                      <span>
                        {row.kind} · {row.status} · {row.data_as_of}
                      </span>
                      <span className="analytical-id">{row.id}</span>
                    </button>
                  );
                })
              ) : (
                <p>
                  No analytical runs. Pin a published source to build the first
                  reconciled SQL result.
                </p>
              )}
              <div className="pagination">
                <button
                  className="button"
                  disabled={busy || list.isFetching || cursors.length === 1}
                  onClick={() => setCursors((value) => value.slice(0, -1))}
                >
                  Previous runs
                </button>
                <button
                  className="button"
                  disabled={busy || list.isFetching || !list.data.next_cursor}
                  onClick={() =>
                    setCursors((value) => [
                      ...value,
                      list.data.next_cursor || undefined,
                    ])
                  }
                >
                  Older runs
                </button>
              </div>
            </>
          )}
        </section>
        {selected ? (
          detail.isPending ? (
            <p role="status">Loading the acknowledged run…</p>
          ) : detail.error ? (
            <ErrorBox error={detail.error} refresh={() => detail.refetch()} />
          ) : (
            <AnalyticsRunResult
              key={detail.data.id}
              run={detail.data}
              busy={busy}
              command={(command) => operation.mutateAsync(command)}
              download={() => downloadAnalyticalManifest(detail.data)}
            />
          )
        ) : (
          <section className="panel analytical-empty">
            <h2>Every total has a source.</h2>
            <p>
              Select an acknowledged run or pin immutable inputs to reconcile
              source counts, financial sums and semantic hashes.
            </p>
          </section>
        )}
      </div>
    </>
  );
}
export function AnalyticsConsole({ me }: { me: Me }) {
  return (
    <div className="analytical-console">
      <div className="page-heading">
        <span className="eyebrow">GOVERNED INTELLIGENCE</span>
        <h1>Analytics & reconciliation</h1>
        <p>
          Versioned SQL results, visible freshness and exact source
          reconciliation.
        </p>
      </div>
      {me.capabilities.includes("analytics.full") ? (
        <FullAnalytics me={me} />
      ) : me.capabilities.includes("analytics.evidence") ? (
        <AuditAnalytics me={me} />
      ) : me.capabilities.includes("analytics.read") ? (
        <ExecutiveAnalytics me={me} />
      ) : (
        <section className="panel">
          <h2>Analytics access is restricted</h2>
          <p>
            Your current grants do not permit analytical totals, raw facts or
            exports.
          </p>
        </section>
      )}
      <p className="analytical-boundary">
        Independent synthetic portfolio · local DuckDB/dbt adapter · Snowflake
        UNVERIFIED · AI OFF
      </p>
    </div>
  );
}
