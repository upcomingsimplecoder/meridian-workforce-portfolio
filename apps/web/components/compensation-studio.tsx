"use client";
import { SnapshotSearch } from "./snapshot-search";
import { useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  compMoney,
  createCycle,
  cycleCommand,
  downloadCompExport,
  getCycle,
  getCycles,
  getEligibility,
  getEvidence,
  getExport,
  getOptions,
  getProposals,
  percent,
  type Cycle,
  type CycleAction,
  type CycleRequest,
} from "@/lib/compensation";
import { CompensationCreate } from "./compensation-create";
import { CompensationGrid } from "./compensation-grid";
import { CompensationReview } from "./compensation-review";

function ErrorBox({ error, retry }: { error: Error; retry: () => void }) {
  return (
    <div className="error-box" role="alert">
      {error.message}
      <button className="button" onClick={retry}>
        Refresh current access
      </button>
    </div>
  );
}

function Eligibility({ me, cycle }: { me: Me; cycle: Cycle }) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const query = useQuery({
    queryKey: ["comp", me.id, "eligibility", cycle.id, cursors.at(-1)],
    queryFn: () => getEligibility(cycle.id, cursors.at(-1)),
  });
  return (
    <details className="comp-eligibility">
      <summary>
        Frozen eligibility · {cycle.configured_population_count} selected /{" "}
        {cycle.ineligible_count} excluded
      </summary>
      {query.isPending ? (
        <p role="status">Loading eligibility evidence…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} retry={() => query.refetch()} />
      ) : (
        query.data && (
          <>
            <ul>
              {query.data.items.map((row) => (
                <li key={row.employee_id}>
                  <strong>{row.employee_id}</strong> ·{" "}
                  {row.eligible
                    ? `Eligible · ${row.proposer_id}`
                    : `Excluded · ${row.reason_codes.join(", ")}`}
                  <code className="comp-hash">{row.sha256}</code>
                </li>
              ))}
            </ul>
            <div className="pagination">
              <button
                className="button"
                disabled={cursors.length === 1}
                onClick={() => setCursors((rows) => rows.slice(0, -1))}
              >
                Previous eligibility page
              </button>
              <button
                className="button"
                disabled={!query.data.next_cursor}
                onClick={() =>
                  setCursors((rows) => [
                    ...rows,
                    query.data.next_cursor ?? undefined,
                  ])
                }
              >
                Next eligibility page
              </button>
            </div>
          </>
        )
      )}
    </details>
  );
}

function ExportPanel({ me, cycle }: { me: Me; cycle: Cycle }) {
  const query = useQuery({
    queryKey: ["comp", me.id, "export", cycle.id],
    queryFn: () => getExport(cycle.id),
    enabled: cycle.export_ready && me.capabilities.includes("comp.export"),
  });
  const download = useMutation({
    mutationFn: () => downloadCompExport(cycle.id),
  });
  if (!cycle.export_ready || !me.capabilities.includes("comp.export"))
    return null;
  return (
    <section className="comp-export">
      <h3>Versioned compensation review export</h3>
      <p>
        Private local CSV and a signed, counted manifest. No payroll or HRIS
        transmission.
      </p>
      {query.isPending ? (
        <p role="status">Verifying the current export…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} retry={() => query.refetch()} />
      ) : (
        query.data && (
          <>
            <dl className="comp-pins">
              <dt>Export version / rows</dt>
              <dd>
                {query.data.version} / {query.data.row_count}
              </dd>
              <dt>CSV SHA-256</dt>
              <dd>{query.data.csv_sha256}</dd>
              <dt>Manifest SHA-256</dt>
              <dd>{query.data.manifest_sha256}</dd>
            </dl>
            <button
              className="button"
              disabled={download.isPending}
              onClick={() => download.mutate()}
            >
              {download.isPending
                ? "Preparing authorized download…"
                : "Download compensation CSV"}
            </button>
            {download.error && (
              <p role="alert" className="error-box">
                {download.error.message}
              </p>
            )}
          </>
        )
      )}
    </section>
  );
}

function CycleDetail({ me, id }: { me: Me; id: string }) {
  const [search, setSearch] = useState("");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [inspect, setInspect] = useState<string>();
  const [reason, setReason] = useState("");
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["comp", me.id, "cycle", id],
    queryFn: () => getCycle(id),
    refetchInterval: (current) =>
      current.state.data?.state === "LOCKED" ? 2000 : false,
  });
  const proposals = useQuery({
    queryKey: ["comp", me.id, "roster", id, cursors.at(-1), search],
    queryFn: () => getProposals(id, cursors.at(-1), search),
    enabled: !!query.data && query.data.state !== "CONFIGURING",
    refetchInterval: (current) =>
      current.state.data?.items.some((row) => !row.verified) ? 2000 : false,
  });
  const command = useMutation({
    mutationFn: (action: CycleAction) => {
      if (!query.data) throw new Error("Reload the acknowledged cycle first.");
      return cycleCommand(me, query.data, action, reason, crypto.randomUUID());
    },
    onSuccess: async (cycle) => {
      setReason("");
      client.setQueryData(["comp", me.id, "cycle", id], cycle);
      await client.invalidateQueries({ queryKey: ["comp", me.id] });
    },
    onError: async () => {
      await query.refetch();
    },
  });
  const cycle = query.data;
  if (query.isPending)
    return <p role="status">Loading the pinned compensation cycle…</p>;
  if (query.error)
    return <ErrorBox error={query.error} retry={() => query.refetch()} />;
  if (!cycle) return null;
  const controller =
    me.id === cycle.owner_id && me.capabilities.includes("comp.manage");
  const nextAction: CycleAction | undefined = !controller
    ? undefined
    : cycle.state === "CONFIGURING"
      ? "launch"
      : cycle.state === "FINAL_APPROVAL"
        ? "lock"
        : cycle.state === "EXPORTED"
          ? "reconcile"
          : cycle.state === "FAILED_EXPORT"
            ? "retry-export"
            : undefined;
  const labels: Record<CycleAction, string> = {
    launch: "Launch frozen cycle",
    lock: "Lock approved cycle",
    reconcile: "Reconcile local export",
    "retry-export": "Retry failed export",
  };
  return (
    <section className="panel comp-detail" aria-labelledby="comp-cycle-heading">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">PINNED COMPENSATION REVIEW</span>
          <h2 id="comp-cycle-heading">{cycle.title}</h2>
          <p>
            <span className="status-tag">
              {cycle.state.replaceAll("_", " ")}
            </span>{" "}
            · version {cycle.version} · {cycle.visible_proposals} authorized
            proposals
          </p>
        </div>
        <button
          className="button"
          onClick={() =>
            client.invalidateQueries({ queryKey: ["comp", me.id] })
          }
        >
          Refresh cycle
        </button>
      </div>
      <div className="comp-cycle-dates">
        <span>
          Eligibility <strong>{cycle.terms.eligibility_date}</strong>
        </span>
        <span>
          Merit starts <strong>{cycle.terms.merit_effective_date}</strong>
        </span>
        <span>
          Bonus service{" "}
          <strong>
            {cycle.terms.bonus_service_start} → {cycle.terms.bonus_service_end}{" "}
            (exclusive)
          </strong>
        </span>
        <span>
          Bonus paid <strong>{cycle.terms.bonus_payment_date}</strong>
        </span>
      </div>
      <div className="comp-budgets">
        {cycle.pools.map((pool) => {
          const ratio = BigInt(pool.budget_minor)
            ? (BigInt(pool.utilized_minor) * 100n) / BigInt(pool.budget_minor)
            : 0n;
          return (
            <section className="comp-budget" key={pool.id}>
              <h3>
                {pool.manager_id} · {pool.org_id} / {pool.currency}
              </h3>
              <p>
                {pool.cost_center} · {pool.award_count} awards
              </p>
              <strong>
                {compMoney(pool.utilized_minor, pool.currency)}{" "}
                <small>of {compMoney(pool.budget_minor, pool.currency)}</small>
              </strong>
              <progress
                value={Number(ratio > 100n ? 100n : ratio)}
                max={100}
                aria-label={`${pool.manager_id} ${pool.currency} pool utilization`}
              />
              <dl>
                <dt>Holdback</dt>
                <dd>{compMoney(pool.holdback_minor, pool.currency)}</dd>
                <dt>Remaining total</dt>
                <dd>{compMoney(pool.remaining_total_minor, pool.currency)}</dd>
                <dt>Release requiring Finance</dt>
                <dd>
                  {compMoney(pool.holdback_required_minor, pool.currency)}
                </dd>
              </dl>
              {[...pool.blockers, ...pool.exceptions, ...pool.warnings].map(
                (code) => (
                  <p className="comp-exception" key={code}>
                    {code.replaceAll("_", " ")}
                  </p>
                ),
              )}
            </section>
          );
        })}
      </div>
      <details className="comp-guidelines">
        <summary>Pinned guidelines and source versions</summary>
        <p>
          Merit guideline {percent(cycle.guideline.merit_guideline_rate)} / hard
          cap {percent(cycle.guideline.merit_hard_cap_rate)}. Bonus guideline{" "}
          {percent(cycle.guideline.bonus_guideline_rate)} / hard cap{" "}
          {percent(cycle.guideline.bonus_hard_cap_rate)}. Minimum active
          service: {cycle.guideline.minimum_service_days} calendar days.{" "}
          {cycle.guideline.rounding}.
        </p>
        <p>
          Merit is an annual full-time increment; the pool reserves its
          annualized FTE cost. The first-year calendar calculation is separate.
          Bonus is a one-time full-time/full-service target prorated once by
          active FTE days.
        </p>
        <dl className="comp-pins">
          <dt>Snapshot</dt>
          <dd>{cycle.snapshot_id}</dd>
          <dt>Guidelines</dt>
          <dd>{cycle.guideline_id}</dd>
          <dt>Guideline SHA-256</dt>
          <dd>{cycle.guideline_sha256}</dd>
          <dt>Reporting FX</dt>
          <dd>{cycle.fx_set_id}</dd>
          <dt>Authorization at launch</dt>
          <dd>{cycle.authz_policy_id}</dd>
          <dt>Frozen inputs SHA-256</dt>
          <dd>{cycle.inputs_sha256}</dd>
          <dt>Current Finance round</dt>
          <dd>{cycle.current_round_id || "Not opened"}</dd>
        </dl>
      </details>
      {nextAction && (
        <form
          className="comp-cycle-command"
          onSubmit={(event) => {
            event.preventDefault();
            command.mutate(nextAction);
          }}
        >
          <label>
            Cycle action reason
            <textarea
              required
              minLength={3}
              maxLength={240}
              disabled={command.isPending}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
            />
          </label>
          <p className="fine-print">
            {nextAction === "lock"
              ? "All current named Finance signatures, budget constraints and independent calculations must pass in one transaction. Locking ends award editing."
              : nextAction === "launch"
                ? "Launch seals the roster, pool allocations and assigned reviewers. Exclusions remain visible below."
                : nextAction === "reconcile"
                  ? "Compare actual signed local files with the locked PostgreSQL revisions, including bytes, counts and original-currency sums."
                  : "Retry the existing failed job after restoring private storage. Locked awards remain sealed."}
          </p>
          <button className="button primary" disabled={command.isPending}>
            {command.isPending ? "Recording cycle action…" : labels[nextAction]}
          </button>
        </form>
      )}
      {command.error && (
        <p role="alert" className="error-box">
          {command.error.message}
        </p>
      )}
      {cycle.state === "LOCKED" && (
        <p role="status">
          Awards locked. The independent worker is producing the counted export.
        </p>
      )}
      {cycle.state === "RECONCILED" && (
        <p role="status" className="comp-reconciled">
          Local export reconciled against immutable source revisions.
        </p>
      )}
      {cycle.state !== "CONFIGURING" && (
        <>
          <div className="comp-roster-heading">
            <h3>Authorized proposals and assigned review inbox</h3>
            <label>
              Search employee ID
              <input
                type="search"
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setCursors([undefined]);
                }}
              />
            </label>
          </div>
          {proposals.isPending ? (
            <p role="status">Loading your authorized roster…</p>
          ) : proposals.error ? (
            <ErrorBox
              error={proposals.error}
              retry={() => proposals.refetch()}
            />
          ) : (
            proposals.data &&
            (proposals.data.items.length ? (
              <>
                <CompensationGrid
                  me={me}
                  cycle={cycle}
                  proposals={proposals.data.items}
                  onInspect={setInspect}
                />
                <div className="pagination">
                  <span>{proposals.data.total} authorized proposals</span>
                  <button
                    className="button"
                    disabled={cursors.length === 1}
                    onClick={() => setCursors((rows) => rows.slice(0, -1))}
                  >
                    Previous proposal page
                  </button>
                  <button
                    className="button"
                    disabled={!proposals.data.next_cursor}
                    onClick={() =>
                      setCursors((rows) => [
                        ...rows,
                        proposals.data.next_cursor ?? undefined,
                      ])
                    }
                  >
                    Next proposal page
                  </button>
                </div>
              </>
            ) : (
              <p>No authorized proposals match this search.</p>
            ))
          )}
          {inspect && (
            <CompensationReview
              key={inspect}
              me={me}
              id={inspect}
              onClose={() => setInspect(undefined)}
            />
          )}
          {me.capabilities.includes("comp.manage") && (
            <Eligibility me={me} cycle={cycle} />
          )}
        </>
      )}
      <ExportPanel me={me} cycle={cycle} />
    </section>
  );
}

function EvidenceList({ me }: { me: Me }) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const query = useQuery({
    queryKey: ["comp", me.id, "evidence", cursors.at(-1)],
    queryFn: () => getEvidence(cursors.at(-1)),
  });
  return (
    <section className="panel comp-evidence">
      <h2>Redacted compensation evidence</h2>
      <p>
        Verify frozen inputs, signatures, export counts and totals within your
        current audit scope.
      </p>
      {query.isPending ? (
        <p role="status">Loading authorized evidence…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} retry={() => query.refetch()} />
      ) : (
        query.data && (
          <>
            {query.data.items.length ? (
              query.data.items.map((row) => (
                <article key={row.cycle_id}>
                  <h3>{row.title}</h3>
                  <p>
                    {row.state} · {row.row_count ?? "Export pending"} rows ·{" "}
                    {row.decision_count} recorded decisions
                  </p>
                  <p>
                    {row.signature_verified === true
                      ? "Local manifest signature verified"
                      : row.signature_verified === false
                        ? "Manifest verification failed"
                        : "No export signature yet"}{" "}
                    ·{" "}
                    {row.reconciled
                      ? "Source/file reconciliation matched"
                      : "Not reconciled"}
                  </p>
                  {row.totals_by_currency &&
                    Object.entries(row.totals_by_currency).map(
                      ([currency, totals]) => (
                        <p key={currency}>
                          <strong>
                            {currency} pool charge{" "}
                            {compMoney(totals.pool_charge_minor, currency)}
                          </strong>{" "}
                          · annualized merit{" "}
                          {compMoney(totals.annualized_merit_minor, currency)} ·
                          first-year merit{" "}
                          {compMoney(totals.merit_first_year_minor, currency)} ·
                          bonus{" "}
                          {compMoney(totals.bonus_payable_minor, currency)}
                        </p>
                      ),
                    )}
                  <dl className="comp-pins">
                    <dt>Cycle</dt>
                    <dd>{row.cycle_id}</dd>
                    <dt>Snapshot</dt>
                    <dd>{row.snapshot_id}</dd>
                    <dt>Guideline / FX</dt>
                    <dd>
                      {row.guideline_id} / {row.fx_set_id}
                    </dd>
                    <dt>Inputs SHA-256</dt>
                    <dd>{row.inputs_sha256}</dd>
                    <dt>Lock SHA-256</dt>
                    <dd>{row.locked_sha256 || "Not locked"}</dd>
                    <dt>Manifest SHA-256</dt>
                    <dd>{row.manifest_sha256 || "Not exported"}</dd>
                  </dl>
                </article>
              ))
            ) : (
              <p>No compensation evidence exists in your current scope.</p>
            )}
            <div className="pagination">
              <button
                className="button"
                disabled={cursors.length === 1}
                onClick={() => setCursors((rows) => rows.slice(0, -1))}
              >
                Previous evidence page
              </button>
              <button
                className="button"
                disabled={!query.data.next_cursor}
                onClick={() =>
                  setCursors((rows) => [
                    ...rows,
                    query.data.next_cursor ?? undefined,
                  ])
                }
              >
                Next evidence page
              </button>
            </div>
          </>
        )
      )}
    </section>
  );
}

export function CompensationStudio({ me }: { me: Me }) {
  const [selected, setSelected] = useState<string>();
  const [create, setCreate] = useState(false);
  const [search, setSearch] = useState("");
  const [sourceSearch, setSourceSearch] = useState("");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const raw =
    me.capabilities.includes("people.pay") &&
    [
      "comp.manage",
      "comp.read",
      "comp.propose",
      "comp.approve",
      "comp.hr.signoff",
      "comp.finance.signoff",
    ].some((cap) => me.capabilities.includes(cap));
  const manage = raw && me.capabilities.includes("comp.manage");
  const client = useQueryClient();
  const list = useQuery({
    queryKey: ["comp", me.id, "cycles", cursors.at(-1), search],
    queryFn: () => getCycles(cursors.at(-1), search),
    enabled: raw,
  });
  const options = useQuery({
    queryKey: ["comp", me.id, "options", sourceSearch],
    queryFn: () => getOptions(sourceSearch),
    placeholderData: keepPreviousData,
    enabled: manage,
  });
  const mutation = useMutation({
    mutationFn: (body: CycleRequest) =>
      createCycle(me, body, crypto.randomUUID()),
    onSuccess: async (cycle) => {
      setSelected(cycle.id);
      setCreate(false);
      client.setQueryData(["comp", me.id, "cycle", cycle.id], cycle);
      await client.invalidateQueries({ queryKey: ["comp", me.id] });
    },
  });
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">EXACT AWARDS · RECORDED DECISIONS</span>
          <h1>Compensation Studio</h1>
          <p>
            Review proposals with frozen inputs, explicit budgets and
            independent signatures.
          </p>
        </div>
        {manage && (
          <button
            className="button primary"
            disabled={!options.data || mutation.isPending}
            onClick={() => setCreate((value) => !value)}
          >
            {create ? "Close cycle configuration" : "New compensation cycle"}
          </button>
        )}
      </div>
      {!raw ? (
        me.capabilities.includes("comp.evidence") ? (
          <EvidenceList me={me} />
        ) : (
          <section className="panel empty-state">
            <h2>Compensation access is restricted.</h2>
            <p>
              Your current permissions do not include raw compensation proposals
              or audit evidence.
            </p>
          </section>
        )
      ) : (
        <>
          {options.error && manage && (
            <ErrorBox error={options.error} retry={() => options.refetch()} />
          )}
          {mutation.error && (
            <ErrorBox error={mutation.error} retry={() => options.refetch()} />
          )}
          {create && options.data && (
            <>
              <SnapshotSearch
                onSearch={setSourceSearch}
                pending={options.isFetching}
              />
              <CompensationCreate
                options={options.data}
                pending={mutation.isPending}
                onCreate={(body) => mutation.mutate(body)}
              />
            </>
          )}
          <section className="panel comp-cycle-list">
            <div className="panel-heading">
              <h2>Persisted compensation cycles</h2>
              <label>
                Search cycles
                <input
                  type="search"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setCursors([undefined]);
                  }}
                />
              </label>
            </div>
            {list.isPending ? (
              <p role="status">Loading authorized cycles…</p>
            ) : list.error ? (
              <ErrorBox error={list.error} retry={() => list.refetch()} />
            ) : (
              list.data && (
                <>
                  {list.data.items.length ? (
                    <ul>
                      {list.data.items.map((row) => (
                        <li key={row.id}>
                          <button
                            className={`comp-cycle-link ${selected === row.id ? "selected" : ""}`}
                            onClick={() => setSelected(row.id)}
                          >
                            <strong>{row.title}</strong>
                            <span>
                              {row.state.replaceAll("_", " ")} ·{" "}
                              {row.visible_proposals} authorized proposals
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>
                      No cycles match your current scope and search.
                      {manage
                        ? " Create a cycle to configure a frozen review."
                        : " An HR controller can launch an assigned cycle."}
                    </p>
                  )}
                  <div className="pagination">
                    <button
                      className="button"
                      disabled={cursors.length === 1}
                      onClick={() => setCursors((rows) => rows.slice(0, -1))}
                    >
                      Previous cycle page
                    </button>
                    <button
                      className="button"
                      disabled={!list.data.next_cursor}
                      onClick={() =>
                        setCursors((rows) => [
                          ...rows,
                          list.data.next_cursor ?? undefined,
                        ])
                      }
                    >
                      Next cycle page
                    </button>
                  </div>
                </>
              )
            )}
          </section>
          {selected && <CycleDetail key={selected} me={me} id={selected} />}
        </>
      )}
    </>
  );
}
