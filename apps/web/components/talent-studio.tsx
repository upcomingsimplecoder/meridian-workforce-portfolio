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
  createTalentCycle,
  downloadTalentExport,
  getTalentCycle,
  getTalentCycles,
  getTalentEvidence,
  getTalentExport,
  getTalentOptions,
  getTalentPacket,
  getTalentPackets,
  talentCycleCommand,
  type TalentCycle,
  type TalentCycleCommand,
} from "@/lib/talent";
import { TalentCreateForm } from "./talent-create";
import { TalentPacketEditor } from "./talent-packet";
function ErrorBox({ error, retry }: { error: Error; retry: () => void }) {
  return (
    <div role="alert" className="error-box">
      {error.message}
      <button className="button" onClick={retry}>
        Refresh current access
      </button>
    </div>
  );
}
function TalentEvidence({ me }: { me: Me }) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const query = useQuery({
    queryKey: ["talent", me.id, "evidence", cursors.at(-1)],
    queryFn: () => getTalentEvidence(cursors.at(-1)),
  });
  return (
    <section className="panel talent-evidence">
      <div className="panel-heading">
        <div>
          <h2>Governance evidence</h2>
          <p>
            Counted locks and source fingerprints. Narrative packets and
            comments remain restricted.
          </p>
        </div>
      </div>
      {query.isPending ? (
        <p role="status">Loading authorized evidence…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} retry={() => query.refetch()} />
      ) : (
        <>
          {query.data.items.length ? (
            query.data.items.map((row) => (
              <article
                className="talent-evidence-row"
                key={row.cycle_id}
                data-cycle-id={row.cycle_id}
              >
                <h3>{row.title}</h3>
                <p>
                  {row.state} · lock {row.lock_number} ·{" "}
                  {row.row_count ?? "No locked"} packets ·{" "}
                  {row.signature_verified
                    ? "Artifact signature verified"
                    : "Export not verified"}
                </p>
                <dl>
                  <dt>Cycle</dt>
                  <dd>{row.cycle_id}</dd>
                  <dt>Snapshot</dt>
                  <dd>{row.snapshot_id}</dd>
                  <dt>Input SHA-256</dt>
                  <dd>{row.inputs_sha256}</dd>
                  <dt>Lock SHA-256</dt>
                  <dd>{row.lock_sha256 || "Not locked"}</dd>
                  <dt>Export SHA-256</dt>
                  <dd>{row.export_sha256 || "Pending"}</dd>
                </dl>
              </article>
            ))
          ) : (
            <p className="talent-empty">
              No review evidence exists within your current scope.
            </p>
          )}
          <Pagination
            cursors={cursors}
            setCursors={setCursors}
            next={query.data.next_cursor}
            label="evidence"
          />
        </>
      )}
    </section>
  );
}
function Pagination({
  cursors,
  setCursors,
  next,
  label,
}: {
  cursors: (string | undefined)[];
  setCursors: (
    fn: (rows: (string | undefined)[]) => (string | undefined)[],
  ) => void;
  next: string | null;
  label: string;
}) {
  return (
    <div className="pagination">
      <button
        className="button"
        disabled={cursors.length === 1}
        onClick={() => setCursors((rows) => rows.slice(0, -1))}
      >
        Previous {label} page
      </button>
      <button
        className="button"
        disabled={!next}
        onClick={() => setCursors((rows) => [...rows, next || undefined])}
      >
        Next {label} page
      </button>
    </div>
  );
}
export function TalentExportPanel({
  me,
  cycle,
}: {
  me: Me;
  cycle: TalentCycle;
}) {
  const [number, setNumber] = useState(cycle.lock_number);
  const awaitingWorker =
    number === cycle.lock_number && !cycle.current_export_ready;
  const query = useQuery({
    queryKey: ["talent", me.id, "export", cycle.id, number, cycle.state],
    queryFn: () => getTalentExport(cycle.id, number),
    enabled: number > 0 && number <= cycle.lock_number && !awaitingWorker,
  });
  const download = useMutation({
    mutationFn: () => downloadTalentExport(cycle.id, number),
  });
  return (
    <section className="talent-export">
      <h3>Immutable review exports</h3>
      <p>
        Local private JSON with two acknowledged human decisions per packet. Its
        HMAC establishes local artifact authenticity.
      </p>
      <label>
        Export lock version
        <input
          type="number"
          min={1}
          max={cycle.lock_number}
          value={number}
          onChange={(e) => {
            const n = Number(e.target.value);
            if (Number.isInteger(n) && n >= 1 && n <= cycle.lock_number)
              setNumber(n);
          }}
        />
      </label>
      {awaitingWorker ? (
        <p role="status">
          {cycle.current_export_failed
            ? "Export worker failed. Restore storage and retry the recorded job."
            : "Independent worker export is pending."}
        </p>
      ) : query.isPending ? (
        <p role="status">Verifying export…</p>
      ) : query.error ? (
        <ErrorBox error={query.error} retry={() => query.refetch()} />
      ) : (
        query.data && (
          <>
            <p
              className={
                query.data.current ? "talent-confirmed" : "talent-historical"
              }
            >
              {query.data.current
                ? "Current locked export"
                : "Historical locked export"}{" "}
              · {query.data.row_count} packets
            </p>
            <dl>
              <dt>Lock SHA-256</dt>
              <dd>{query.data.lock_sha256}</dd>
              <dt>Export SHA-256</dt>
              <dd>{query.data.export_sha256}</dd>
            </dl>
            <button
              className="button"
              disabled={download.isPending}
              onClick={() => download.mutate()}
            >
              Download review JSON
            </button>
          </>
        )
      )}
      {download.error && (
        <p role="alert" className="error-box">
          {download.error.message}
        </p>
      )}
    </section>
  );
}
function CycleWorkspace({
  me,
  id,
  onBack,
}: {
  me: Me;
  id: string;
  onBack: () => void;
}) {
  const client = useQueryClient();
  const [packetId, setPacketId] = useState<string>();
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [search, setSearch] = useState("");
  const [reason, setReason] = useState("");
  const cycle = useQuery({
    queryKey: ["talent", me.id, "cycle", id],
    queryFn: () => getTalentCycle(id),
    refetchInterval: (q) =>
      q.state.data?.state === "LOCKED" &&
      !q.state.data.current_export_ready &&
      !q.state.data.current_export_failed
        ? 2000
        : false,
  });
  const queue = useQuery({
    queryKey: ["talent", me.id, "queue", id, cursors.at(-1), search],
    queryFn: () => getTalentPackets(id, cursors.at(-1), search),
    enabled: !!cycle.data && cycle.data.state !== "CONFIGURING",
  });
  const packet = useQuery({
    queryKey: ["talent", me.id, "packet", packetId],
    queryFn: () => getTalentPacket(packetId!),
    enabled: !!packetId,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  function acknowledge() {
    void client.invalidateQueries({ queryKey: ["talent", me.id, "cycle", id] });
    void client.invalidateQueries({ queryKey: ["talent", me.id, "queue", id] });
  }
  const action = useMutation({
    mutationFn: (command: TalentCycleCommand) =>
      talentCycleCommand(me, cycle.data!, command, crypto.randomUUID()),
    onSuccess: (data) => {
      client.setQueryData(["talent", me.id, "cycle", id], data);
      acknowledge();
      void client.invalidateQueries({ queryKey: ["talent", me.id, "packet"] });
    },
  });
  if (cycle.isPending) return <p role="status">Loading frozen review…</p>;
  if (cycle.error)
    return <ErrorBox error={cycle.error} retry={() => cycle.refetch()} />;
  const data = cycle.data;
  const fullySigned =
    data.visible_packets > 0 &&
    (data.states.APPROVED || 0) + (data.states.LOCKED || 0) ===
      data.visible_packets;
  return (
    <>
      <div className="talent-cycle-header">
        <button className="button" disabled={!!packetId} onClick={onBack}>
          Back to review cycles
        </button>
        <div>
          <h2>{data.title}</h2>
          <p>
            {data.state} · effective {data.as_of} · deadline {data.due_date} ·
            version {data.version}
          </p>
        </div>
        <span className="status-tag">
          {data.visible_packets} visible packets
        </span>
      </div>
      <details className="panel talent-pins">
        <summary>Frozen source and policy</summary>
        <dl>
          <dt>Snapshot</dt>
          <dd>{data.snapshot_id}</dd>
          <dt>Snapshot SHA-256</dt>
          <dd>{data.snapshot_sha256}</dd>
          <dt>Narrative policy</dt>
          <dd>{data.policy_id}</dd>
          <dt>Authorization policy</dt>
          <dd>{data.authz_policy_id}</dd>
          <dt>Input SHA-256</dt>
          <dd>{data.inputs_sha256}</dd>
        </dl>
      </details>
      {data.can_control && !packetId && (
        <div className="panel talent-cycle-actions">
          <label>
            Cycle action reason
            <input
              minLength={3}
              maxLength={240}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={action.isPending}
            />
          </label>
          <div className="talent-actions">
            {data.state === "CONFIGURING" && (
              <button
                className="button primary"
                disabled={reason.trim().length < 3 || action.isPending}
                onClick={() =>
                  action.mutate({ action: "launch", body: { reason } })
                }
              >
                Launch frozen review
              </button>
            )}
            {data.state === "ACTIVE" && (
              <button
                className="button primary"
                disabled={
                  !fullySigned || reason.trim().length < 3 || action.isPending
                }
                onClick={() =>
                  action.mutate({ action: "lock", body: { reason } })
                }
              >
                Lock signed review
              </button>
            )}
            {data.current_export_failed && (
              <button
                className="button"
                disabled={reason.trim().length < 3 || action.isPending}
                onClick={() =>
                  action.mutate({ action: "retry-export", body: { reason } })
                }
              >
                Retry failed review export
              </button>
            )}
          </div>
          {data.state === "ACTIVE" && !fullySigned && (
            <p>
              Each current packet needs independent calibration and HR
              acknowledgement before locking.
            </p>
          )}
          {data.state === "LOCKED" &&
            !data.current_export_ready &&
            !data.current_export_failed && (
              <p role="status">
                Signed packets are locked. Independent export verification is
                pending.
              </p>
            )}
          {action.error && (
            <ErrorBox error={action.error} retry={() => cycle.refetch()} />
          )}
        </div>
      )}
      {packetId ? (
        packet.isPending ? (
          <p role="status">Loading authorized packet…</p>
        ) : packet.error ? (
          <>
            <ErrorBox error={packet.error} retry={() => packet.refetch()} />
            <button className="button" onClick={() => setPacketId(undefined)}>
              Close unavailable packet
            </button>
          </>
        ) : (
          packet.data && (
            <TalentPacketEditor
              key={packetId}
              me={me}
              initial={packet.data}
              onAcknowledged={(data) => {
                client.setQueryData(
                  ["talent", me.id, "packet", packetId],
                  data,
                );
                acknowledge();
              }}
              onClose={() => setPacketId(undefined)}
            />
          )
        )
      ) : (
        data.state !== "CONFIGURING" && (
          <section className="panel talent-queue">
            <div className="panel-heading">
              <div>
                <h2>Reviewer queue</h2>
                <p>
                  Current rights intersect the frozen assignments and source
                  hierarchy.
                </p>
              </div>
              <button
                className="button"
                onClick={() => {
                  acknowledge();
                }}
              >
                Refresh queue
              </button>
            </div>
            <div className="talent-search">
              <label>
                Search packet by synthetic ID
                <input
                  type="search"
                  value={search}
                  maxLength={100}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setCursors([undefined]);
                  }}
                />
              </label>
            </div>
            {queue.isPending ? (
              <p role="status">Loading scoped packets…</p>
            ) : queue.error ? (
              <ErrorBox error={queue.error} retry={() => queue.refetch()} />
            ) : (
              queue.data && (
                <>
                  <div
                    className="talent-table-scroll"
                    role="region"
                    aria-label="Reviewer queue table"
                    tabIndex={0}
                  >
                    <table className="data-table">
                      <caption>
                        {queue.data.total} authorized packets matching this
                        filter
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">Synthetic person</th>
                          <th scope="col">Current state</th>
                          <th scope="col">Revision</th>
                          <th scope="col">Human route</th>
                          <th scope="col">Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {queue.data.items.map((row) => (
                          <tr key={row.id}>
                            <th scope="row">
                              {row.display_name}
                              <small>{row.employee_id}</small>
                            </th>
                            <td>{row.state}</td>
                            <td>{row.current_revision}</td>
                            <td>
                              {row.author_id} → {row.calibrator_id} →{" "}
                              {row.hr_signer_id}
                            </td>
                            <td>
                              <button
                                className="text-button"
                                aria-label={`Open packet ${row.employee_id}`}
                                onClick={() => setPacketId(row.id)}
                              >
                                Open packet
                              </button>
                              {data.can_control && row.state === "LOCKED" && (
                                <button
                                  className="text-button"
                                  disabled={
                                    reason.trim().length < 3 || action.isPending
                                  }
                                  aria-label={`Reopen packet ${row.employee_id}`}
                                  onClick={() =>
                                    action.mutate({
                                      action: "reopen",
                                      body: { packet_id: row.id, reason },
                                    })
                                  }
                                >
                                  Reopen as revision
                                </button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {!queue.data.items.length && (
                    <p className="talent-empty">
                      No assigned packets match your current access and filter.
                    </p>
                  )}
                  <Pagination
                    cursors={cursors}
                    setCursors={setCursors}
                    next={queue.data.next_cursor}
                    label="packet"
                  />
                </>
              )
            )}
          </section>
        )
      )}
      {data.population_count !== null && data.lock_number > 0 && !packetId && (
        <TalentExportPanel key={data.lock_number} me={me} cycle={data} />
      )}
    </>
  );
}
export function TalentStudio({ me }: { me: Me }) {
  const [id, setId] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  const [sourceSearch, setSourceSearch] = useState("");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const client = useQueryClient();
  const raw =
    me.capabilities.includes("people.read") &&
    ["reviews.manage", "reviews.write", "reviews.approve"].some((c) =>
      me.capabilities.includes(c),
    );
  const cycles = useQuery({
    queryKey: ["talent", me.id, "cycles", cursors.at(-1), search],
    queryFn: () => getTalentCycles(cursors.at(-1), search),
    enabled: raw && !id,
  });
  const options = useQuery({
    queryKey: ["talent", me.id, "options", sourceSearch],
    queryFn: () => getTalentOptions(sourceSearch),
    placeholderData: keepPreviousData,
    enabled: creating && me.capabilities.includes("reviews.manage"),
  });
  const create = useMutation({
    mutationFn: (body: Parameters<typeof createTalentCycle>[1]) =>
      createTalentCycle(me, body, crypto.randomUUID()),
    onSuccess: (data) => {
      client.setQueryData(["talent", me.id, "cycle", data.id], data);
      setId(data.id);
      setCreating(false);
      void client.invalidateQueries({ queryKey: ["talent", me.id, "cycles"] });
    },
  });
  return (
    <div className="talent-studio">
      <div className="page-heading">
        <div>
          <div className="eyebrow">QUALITATIVE REVIEW & CALIBRATION</div>
          <h1>Talent Review</h1>
          <p>
            Human observations, independent review and revisions you can trace.
          </p>
        </div>
        {raw && me.capabilities.includes("reviews.manage") && !id && (
          <button
            className="button primary"
            onClick={() => setCreating((v) => !v)}
          >
            {creating ? "Close configuration" : "New talent cycle"}
          </button>
        )}
      </div>
      <p className="talent-policy-note">
        Synthetic demonstration only. Professional human review is required for
        real employment policies. No employee scoring, ranking or AI personnel
        decisions.
      </p>
      {!raw ? (
        me.capabilities.includes("audit.read") ? (
          <TalentEvidence me={me} />
        ) : (
          <section className="panel talent-empty">
            <h2>Review access restricted</h2>
            <p>
              Your current role does not permit raw qualitative packets or
              review exports.
            </p>
          </section>
        )
      ) : id ? (
        <CycleWorkspace
          key={id}
          me={me}
          id={id}
          onBack={() => setId(undefined)}
        />
      ) : (
        <>
          {creating &&
            (options.isPending ? (
              <p role="status">Loading permitted review options…</p>
            ) : options.error ? (
              <ErrorBox error={options.error} retry={() => options.refetch()} />
            ) : (
              options.data && (
                <>
                  <SnapshotSearch
                    onSearch={setSourceSearch}
                    pending={options.isFetching}
                  />
                  <TalentCreateForm
                    options={options.data}
                    pending={create.isPending}
                    onCreate={(body) => create.mutate(body)}
                  />
                </>
              )
            ))}
          {create.error && (
            <ErrorBox error={create.error} retry={() => options.refetch()} />
          )}
          <section className="panel talent-cycle-list">
            <div className="panel-heading">
              <div>
                <h2>Review cycles</h2>
                <p>Frozen populations with current scoped access.</p>
              </div>
            </div>
            <div className="talent-search">
              <label>
                Search review cycles
                <input
                  type="search"
                  value={search}
                  maxLength={100}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setCursors([undefined]);
                  }}
                />
              </label>
            </div>
            {cycles.isPending ? (
              <p role="status">Loading review cycles…</p>
            ) : cycles.error ? (
              <ErrorBox error={cycles.error} retry={() => cycles.refetch()} />
            ) : (
              cycles.data && (
                <>
                  {cycles.data.items.length ? (
                    cycles.data.items.map((row) => (
                      <button
                        key={row.id}
                        className="talent-cycle-link"
                        onClick={() => setId(row.id)}
                      >
                        <strong>{row.title}</strong>
                        <span>
                          {row.state} · {row.visible_packets} visible packets ·
                          due {row.due_date}
                        </span>
                      </button>
                    ))
                  ) : (
                    <p className="talent-empty">
                      No review cycles match your current access and search.
                    </p>
                  )}
                  <Pagination
                    cursors={cursors}
                    setCursors={setCursors}
                    next={cycles.data.next_cursor}
                    label="review"
                  />
                </>
              )
            )}
          </section>
        </>
      )}
    </div>
  );
}
