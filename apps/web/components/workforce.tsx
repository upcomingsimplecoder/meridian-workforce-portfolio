"use client";
import { useCallback, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  LockKeyhole,
  Search,
  ShieldCheck,
  Users,
} from "lucide-react";
import type { Me } from "@/lib/api";
import {
  exportPeople,
  getOrgTree,
  getPeople,
  getSnapshots,
  getSummary,
  type HistoricalFilters,
  type Snapshot,
  type WorkforceSummary,
} from "@/lib/workforce";
import { WorkforceTable } from "./workforce-table";
import { PersonDetail } from "./person-detail";
import { SnapshotSearch } from "./snapshot-search";

type InitialSource = { snapshot?: string; asOf?: string };
export function WorkforceContent({
  me,
  initial,
}: {
  me: Me;
  initial?: InitialSource;
}) {
  if (
    !me.capabilities.some((cap) =>
      ["people.read", "analytics.read"].includes(cap),
    )
  )
    return (
      <section className="panel empty-state">
        <LockKeyhole size={28} />
        <h1>Workforce access is restricted</h1>
        <p>
          Your current grants do not include people or workforce aggregates.
          Audit access is governed separately.
        </p>
      </section>
    );
  return <Explorer key={me.id} me={me} initial={initial} />;
}
function Explorer({ me, initial }: { me: Me; initial?: InitialSource }) {
  const [sourceSearch, setSourceSearch] = useState("");
  const snapshots = useQuery({
    queryKey: ["snapshots", me.id, sourceSearch],
    queryFn: () => getSnapshots(sourceSearch),
    placeholderData: keepPreviousData,
  });
  const [snapshotSelection, setSnapshot] = useState(initial?.snapshot || "");
  const [asOf, setAsOf] = useState(initial?.asOf || "2027-06-30");
  const snapshotId = snapshotSelection || snapshots.data?.[0]?.id || "";
  const filters = { snapshot_id: snapshotId, as_of: asOf };
  const metrics = useQuery({
    queryKey: ["workforce-summary", me.id, filters],
    queryFn: () => getSummary(filters),
    enabled: !!snapshotId,
  });
  const selected =
    metrics.data?.snapshot ||
    snapshots.data?.find((row) => row.id === snapshotId);
  const aggregateOnly = !me.capabilities.includes("people.read");
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">PEOPLE & ORGANIZATION</div>
          <h1>
            Workforce Explorer<span className="heading-dot">.</span>
          </h1>
          <p>
            Explore the workforce as it was known, at the date that matters.
          </p>
        </div>
        <span className="status-pill">
          <span className="status-dot" /> Synthetic data only
        </span>
      </div>
      <SnapshotSearch
        onSearch={setSourceSearch}
        pending={snapshots.isFetching}
      />
      <div className="workforce-context panel">
        <label>
          Source snapshot
          <select
            aria-label="Source snapshot"
            value={snapshotId}
            onChange={(event) => setSnapshot(event.target.value)}
            disabled={snapshots.isPending}
          >
            {snapshots.data?.map((row) => (
              <option key={row.id} value={row.id}>
                {row.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Effective date
          <input
            aria-label="Effective date"
            type="date"
            value={asOf}
            onChange={(event) => {
              if (event.target.value) setAsOf(event.target.value);
            }}
            min="1900-01-01"
            max="2200-12-31"
          />
        </label>
        <div className="context-help">
          <ShieldCheck size={18} />
          <span>
            History is immutable.
            <small>Change the snapshot to compare what was known.</small>
          </span>
        </div>
      </div>
      {snapshots.error ? (
        <LoadError error={snapshots.error} retry={() => snapshots.refetch()} />
      ) : snapshots.isPending ? (
        <Loading />
      ) : !snapshots.data.length ? (
        <section className="panel empty-state">
          <Users size={28} />
          <h2>
            {sourceSearch
              ? "No matching snapshots"
              : "No published workforce yet"}
          </h2>
          <p>
            {sourceSearch
              ? "Try another source title, or clear the search to see recent snapshots."
              : "A data steward must publish a validated source before this view becomes available."}
          </p>
        </section>
      ) : (
        <>
          {metrics.error ? (
            <LoadError error={metrics.error} retry={() => metrics.refetch()} />
          ) : metrics.isPending ? (
            <Loading />
          ) : (
            <>
              <div className="scope-strip">
                <ShieldCheck size={16} />
                <span>{metrics.data.scope_label}</span>
                <span className="strip-end">Policy {me.policy_version}</span>
              </div>
              <Metrics summary={metrics.data} />
            </>
          )}
          {aggregateOnly ? (
            metrics.data &&
            !metrics.error && <AggregateView summary={metrics.data} />
          ) : (
            <PeopleResults key={me.id} me={me} filters={filters} />
          )}
          {selected && <Provenance snapshot={selected} asOf={asOf} />}
        </>
      )}
    </>
  );
}
function Metrics({ summary }: { summary: WorkforceSummary }) {
  return (
    <section className="workforce-metrics" aria-label="Workforce totals">
      <div>
        <span>Active headcount</span>
        <strong data-testid="headcount">
          {summary.headcount === null
            ? "Suppressed"
            : summary.headcount.toLocaleString("en-US")}
        </strong>
        <small>People in your permitted scope</small>
      </div>
      <div>
        <span>Full-time equivalent</span>
        <strong data-testid="fte">
          {summary.fte === null ? "Suppressed" : summary.fte}
        </strong>
        <small>Exact sum of active assignments</small>
      </div>
      <div>
        <span>Department coverage</span>
        <strong>
          {summary.suppressed
            ? "Suppressed"
            : summary.departments.length.toString().padStart(2, "0")}
        </strong>
        <small>
          {summary.suppressed
            ? "Small-cohort privacy rules applied"
            : "At the selected effective date"}
        </small>
      </div>
      <div className="metric-date">
        <span>Effective on</span>
        <strong>{summary.as_of}</strong>
        <small>Definition {summary.metric_definition_version}</small>
      </div>
    </section>
  );
}
type PeopleContext = {
  snapshot: string;
  date: string;
  org: string;
  cursors: (string | undefined)[];
  selected: string | null;
  exportState: string;
  exportTask: object | null;
};
function peopleContext(filters: HistoricalFilters): PeopleContext {
  return {
    snapshot: filters.snapshot_id,
    date: filters.as_of,
    org: "",
    cursors: [undefined],
    selected: null,
    exportState: "",
    exportTask: null,
  };
}
function PeopleResults({
  me,
  filters,
}: {
  me: Me;
  filters: HistoricalFilters;
}) {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [storedContext, setContext] = useState(() => peopleContext(filters));
  // Keep the live input mounted, but never reuse a cursor/detail from another
  // historical context. Derive the reset before issuing the new query.
  const context =
    storedContext.snapshot === filters.snapshot_id &&
    storedContext.date === filters.as_of
      ? storedContext
      : peopleContext(filters);
  if (context !== storedContext) setContext(context);
  const { org, cursors, selected, exportState } = context;
  const selectPerson = useCallback(
    (id: string) => setContext((previous) => ({ ...previous, selected: id })),
    [],
  );
  const applied = {
    ...filters,
    q: query,
    org: org || undefined,
    cursor: cursors.at(-1),
    limit: 25,
  };
  const people = useQuery({
    queryKey: ["people", me.id, applied],
    queryFn: () => getPeople(applied),
  });
  const tree = useQuery({
    queryKey: ["org-tree", me.id, filters],
    queryFn: () => getOrgTree(filters),
  });
  function chooseOrg(id: string) {
    setContext((previous) => ({
      ...previous,
      org: id,
      cursors: [undefined],
      exportState: "",
      exportTask: null,
    }));
  }
  async function download() {
    // Retain the initiating context so a delayed completion cannot label a
    // different source/date as exported. The API reauthorizes the actual file.
    const task = {};
    setContext((previous) => ({
      ...previous,
      exportState: "Preparing export…",
      exportTask: task,
    }));
    const complete = (message: string) =>
      setContext((previous) =>
        previous.exportTask === task
          ? { ...previous, exportState: message, exportTask: null }
          : previous,
      );
    try {
      await exportPeople({ ...applied, cursor: undefined });
      complete("Export downloaded for your current permitted scope.");
    } catch (error) {
      complete(
        error instanceof Error
          ? error.message
          : "Export failed. Please try again.",
      );
    }
  }
  const groups = tree.data?.filter((node) => node.parent_id === null) || [];
  return (
    <div className="explorer-grid">
      <aside className="org-panel panel" aria-label="Organization hierarchy">
        <div className="eyebrow">ORGANIZATION</div>
        <h2>Your visible hierarchy</h2>
        <button
          className={`org-option ${!org ? "selected" : ""}`}
          aria-pressed={!org}
          onClick={() => chooseOrg("")}
        >
          All permitted people
        </button>
        {tree.isPending ? (
          <p role="status">Loading departments…</p>
        ) : tree.error ? (
          <LoadError error={tree.error} retry={() => tree.refetch()} />
        ) : (
          groups.map((group) => (
            <div className="org-group" key={group.id}>
              <h3>{group.name}</h3>
              {tree.data
                ?.filter((node) => node.parent_id === group.id)
                .map((node) => (
                  <button
                    key={node.id}
                    className={`org-option ${org === node.id ? "selected" : ""}`}
                    aria-pressed={org === node.id}
                    onClick={() => chooseOrg(node.id)}
                  >
                    <span>{node.name}</span>
                    <small>{node.id}</small>
                  </button>
                ))}
            </div>
          ))
        )}
        <p className="org-note">
          The hierarchy shows departments represented in your reporting scope. A
          department filter only narrows your access.
        </p>
      </aside>
      <section className="people-panel panel" aria-label="People explorer">
        <div className="people-toolbar">
          <form
            role="search"
            onSubmit={(event) => {
              event.preventDefault();
              setQuery(search.trim());
              setContext((previous) => ({
                ...previous,
                cursors: [undefined],
                exportState: "",
                exportTask: null,
              }));
            }}
          >
            <label className="sr-only" htmlFor="people-search">
              Search people
            </label>
            <Search size={16} aria-hidden="true" />
            <input
              id="people-search"
              placeholder="Search by name or synthetic ID"
              maxLength={80}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <button className="button" type="submit">
              Search
            </button>
          </form>
          {me.capabilities.includes("people.export") && (
            <button
              className="button"
              onClick={download}
              disabled={exportState === "Preparing export…"}
            >
              <Download size={15} /> Export matching
            </button>
          )}
        </div>
        <label className="mobile-department">
          Department
          <select
            aria-label="Department"
            value={org}
            onChange={(event) => chooseOrg(event.target.value)}
          >
            <option value="">All permitted people</option>
            {tree.data
              ?.filter((node) => node.parent_id !== null)
              .map((node) => (
                <option value={node.id} key={node.id}>
                  {node.name}
                </option>
              ))}
          </select>
        </label>
        {exportState && (
          <p className="export-message" role="status">
            {exportState}
          </p>
        )}
        {people.isPending ? (
          <Loading />
        ) : people.error ? (
          <LoadError error={people.error} retry={() => people.refetch()} />
        ) : (
          <>
            <div className="results-label">
              <span>
                <strong>{people.data.total.toLocaleString("en-US")}</strong>{" "}
                matching people{org ? ` · ${org}` : ""}
              </span>
              <span>
                {people.data.can_view_pay
                  ? "Pay in original currency · no FX conversion"
                  : "Restricted fields excluded"}
              </span>
            </div>
            {!people.data.items.length ? (
              <div className="empty-state people-empty">
                <Search size={25} />
                <h2>No matching people</h2>
                <p>
                  Try another name, department, effective date, or snapshot.
                  Results always remain within your permitted scope.
                </p>
                <button
                  className="button"
                  onClick={() => {
                    setSearch("");
                    setQuery("");
                    chooseOrg("");
                  }}
                >
                  Clear search and department
                </button>
              </div>
            ) : (
              <WorkforceTable
                people={people.data.items}
                showPay={people.data.can_view_pay}
                onSelect={selectPerson}
              />
            )}
            <div className="pagination">
              <span>Page {cursors.length} · Up to 25 records per page</span>
              <div>
                <button
                  className="button"
                  disabled={cursors.length === 1}
                  onClick={() =>
                    setContext((previous) => ({
                      ...previous,
                      cursors: previous.cursors.slice(0, -1),
                    }))
                  }
                >
                  <ChevronLeft size={15} /> Previous
                </button>
                <button
                  className="button"
                  disabled={!people.data.next_cursor}
                  onClick={() =>
                    setContext((previous) => ({
                      ...previous,
                      cursors: [
                        ...previous.cursors,
                        people.data.next_cursor || undefined,
                      ],
                    }))
                  }
                >
                  Next <ChevronRight size={15} />
                </button>
              </div>
            </div>
          </>
        )}
      </section>
      {selected && (
        <PersonDetail
          id={selected}
          principal={me.id}
          filters={filters}
          onClose={() =>
            setContext((previous) => ({ ...previous, selected: null }))
          }
        />
      )}
    </div>
  );
}
function AggregateView({ summary }: { summary: WorkforceSummary }) {
  const largest = Math.max(
    1,
    ...summary.departments.map((org) => org.headcount),
  );
  return (
    <section className="panel aggregate-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">GOVERNED WORKFORCE TOTALS</span>
          <h2>Department distribution</h2>
        </div>
        <span className="privacy-label">
          <LockKeyhole size={15} /> Aggregate access
        </span>
      </div>
      <p className="section-copy">
        Headcount at the selected snapshot and effective date. Individual
        records and pay are restricted. Cohorts smaller than five suppress the
        full breakdown.
      </p>
      {summary.suppressed ? (
        <p className="privacy-notice" role="status">
          Department breakdown withheld by the small-cohort policy.
        </p>
      ) : (
        <div
          className="department-bars"
          role="list"
          aria-label="Department headcounts"
        >
          {summary.departments.map((org) => (
            <div className="department-bar" role="listitem" key={org.id}>
              <span>{org.name}</span>
              <div aria-hidden="true">
                <span
                  style={{ width: `${(org.headcount / largest) * 100}%` }}
                />
              </div>
              <strong>{org.headcount.toLocaleString("en-US")}</strong>
            </div>
          ))}
        </div>
      )}
      <p className="detail-note">
        Calculated {new Date(summary.calculated_at).toLocaleString("en-US")} ·{" "}
        {summary.metric_definition_version}
      </p>
    </section>
  );
}
function Provenance({ snapshot, asOf }: { snapshot: Snapshot; asOf: string }) {
  return (
    <details className="source-provenance">
      <summary>
        Source provenance · {snapshot.title} · Effective {asOf}
      </summary>
      <dl className="evidence-list">
        <dt>Snapshot</dt>
        <dd>{snapshot.id}</dd>
        <dt>Source batch</dt>
        <dd>{snapshot.source_batch_id}</dd>
        <dt>Watermark</dt>
        <dd>{snapshot.source_watermark}</dd>
        <dt>Recorded</dt>
        <dd>{snapshot.captured_at}</dd>
        <dt>SHA-256</dt>
        <dd>{snapshot.sha256}</dd>
      </dl>
    </details>
  );
}
function Loading() {
  return (
    <div className="panel loading-panel" role="status">
      Loading workforce from the selected source…
    </div>
  );
}
function LoadError({ error, retry }: { error: Error; retry: () => void }) {
  return (
    <div className="error-box" role="alert">
      <h2>Workforce is unavailable</h2>
      <p>{error.message}</p>
      <button className="button" onClick={retry}>
        Retry workforce
      </button>
    </div>
  );
}
