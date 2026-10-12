"use client";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  captureReplay,
  downloadReplay,
  getReplay,
  getReplayChoices,
  getReplayList,
  verifyReplay,
  type Replay,
  type ReplayRequest,
} from "@/lib/replay";
import { ReplayDetail } from "./replay-detail";
import { AuditExplorer } from "./audit-explorer";

function EvidenceWorkspace({ me }: { me: Me }) {
  const params = useSearchParams();
  const [kind, setKind] = useState<ReplayRequest["kind"]>(
    params.get("kind") === "AWARD" ? "AWARD" : "SCENARIO",
  );
  const [subject, setSubject] = useState(params.get("subject") || "");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [reason, setReason] = useState("");
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const client = useQueryClient();
  const prefix = ["replay", me.id];
  const choices = useQuery({
    queryKey: [...prefix, "choices", kind, search],
    queryFn: () => getReplayChoices(kind, search),
    gcTime: 0,
  });
  const list = useQuery({
    queryKey: [...prefix, "list", subject, cursors.at(-1)],
    queryFn: () => getReplayList(cursors.at(-1), subject),
    gcTime: 0,
  });
  const detail = useQuery({
    queryKey: [...prefix, "detail", selected],
    queryFn: () => getReplay(selected),
    enabled: !!selected,
    gcTime: 0,
    staleTime: 10000,
  });
  async function acknowledged(replay: Replay) {
    client.setQueryData([...prefix, "detail", replay.id], replay);
    setSelected(replay.id);
    setCursors([undefined]);
    await client.invalidateQueries({ queryKey: [...prefix, "list"] });
  }
  const capture = useMutation({
    mutationFn: () =>
      captureReplay(
        me,
        { kind, subject_id: subject, reason: reason.trim() },
        crypto.randomUUID(),
      ),
    onSuccess: acknowledged,
  });
  const verify = useMutation({
    mutationFn: (reason: string) => {
      if (!detail.data)
        throw new Error("Reload the selected evidence before verifying.");
      return verifyReplay(me, detail.data, reason, crypto.randomUUID());
    },
    onSuccess: acknowledged,
  });
  const busy = capture.isPending || verify.isPending;
  return (
    <>
      <section className="panel replay-capture">
        <h2>Choose a persisted decision</h2>
        <p>
          Capture verified calculation inputs, the comparison baseline, human
          decisions and event fingerprints at this point in time. The original
          records remain immutable.
        </p>
        <form
          className="replay-search"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(searchInput.trim());
          }}
        >
          <label>
            Search authorized versions
            <input
              aria-label="Search authorized versions"
              maxLength={100}
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
            />
          </label>
          <button className="button" disabled={busy}>
            Search versions
          </button>
        </form>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setError("");
            try {
              await capture.mutateAsync();
            } catch (failure) {
              setError(
                failure instanceof Error
                  ? failure.message
                  : "The capsule was not acknowledged. Your selection is retained.",
              );
            }
          }}
        >
          <fieldset disabled={busy}>
            <div className="replay-fields">
              <label>
                Decision type
                <select
                  aria-label="Decision type"
                  value={kind}
                  onChange={(event) => {
                    setKind(event.target.value as ReplayRequest["kind"]);
                    setSubject("");
                    setSearch("");
                    setSearchInput("");
                    setSelected("");
                    setCursors([undefined]);
                  }}
                >
                  <option value="SCENARIO">12-month scenario cost</option>
                  <option value="AWARD">Compensation award</option>
                </select>
              </label>
              <label>
                Verified decision version
                <select
                  aria-label="Verified decision version"
                  required
                  value={subject}
                  onChange={(event) => {
                    setSubject(event.target.value);
                    setCursors([undefined]);
                    setSelected("");
                  }}
                >
                  <option value="">Choose an authorized version</option>
                  {subject &&
                    !choices.data?.some(
                      (row) => row.subject_id === subject,
                    ) && (
                      <option value={subject}>Linked revision {subject}</option>
                    )}
                  {choices.data?.map((row) => (
                    <option key={row.subject_id} value={row.subject_id}>
                      {row.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              Evidence capture reason
              <textarea
                aria-label="Evidence capture reason"
                required
                minLength={3}
                maxLength={240}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            <button
              className="button primary"
              disabled={!subject || reason.trim().length < 3}
            >
              {capture.isPending
                ? "Capturing and verifying…"
                : "Capture and verify decision"}
            </button>
          </fieldset>
        </form>
        {choices.isPending && <p role="status">Loading authorized versions…</p>}
        {choices.error && (
          <p role="alert" className="error-box">
            {choices.error.message}
            <button className="button" onClick={() => choices.refetch()}>
              Refresh authorized choices
            </button>
          </p>
        )}
        {choices.data?.length === 0 && (
          <p>
            No verified {kind === "AWARD" ? "awards" : "forecasts"} match this
            search within your current scope.
          </p>
        )}
        {error && (
          <p role="alert" className="error-box">
            {error}
          </p>
        )}
      </section>
      <section className="panel replay-history">
        <div className="panel-heading">
          <h2>Captured evidence</h2>
          <button
            className="button"
            disabled={busy}
            onClick={() => {
              void list.refetch();
              if (selected) void detail.refetch();
            }}
          >
            Refresh persisted evidence
          </button>
        </div>
        {list.isPending ? (
          <p role="status">Loading persisted capsules…</p>
        ) : list.error ? (
          <p role="alert" className="error-box">
            {list.error.message}
          </p>
        ) : list.data.items.length ? (
          <ul className="replay-choices">
            {list.data.items.map((row) => (
              <li key={row.id}>
                <button
                  className={selected === row.id ? "button selected" : "button"}
                  aria-pressed={selected === row.id}
                  data-capsule-id={row.id}
                  onClick={() => {
                    setSelected(row.id);
                    setError("");
                  }}
                >
                  {row.kind} · {row.id.slice(0, 8)} ·{" "}
                  {new Date(row.created_at).toLocaleString()}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p>
            No captured evidence for this selection. Choose a verified decision
            and explain the capture purpose.
          </p>
        )}
        <div className="pagination">
          <button
            className="button"
            disabled={busy || cursors.length < 2}
            onClick={() => setCursors((value) => value.slice(0, -1))}
          >
            Previous evidence page
          </button>
          <span>Page {cursors.length}</span>
          <button
            className="button"
            disabled={busy || !list.data?.next_cursor}
            onClick={() =>
              setCursors((value) => [
                ...value,
                list.data?.next_cursor || undefined,
              ])
            }
          >
            Next evidence page
          </button>
        </div>
      </section>
      {selected &&
        (detail.isPending ? (
          <p role="status">Loading selected frozen evidence…</p>
        ) : detail.error ? (
          <p role="alert" className="error-box">
            {detail.error.message}
            <button className="button" onClick={() => detail.refetch()}>
              Refresh current evidence access
            </button>
          </p>
        ) : (
          <ReplayDetail
            key={detail.data.id}
            replay={detail.data}
            busy={busy}
            verify={(reason) => verify.mutateAsync(reason)}
            download={(attempt) => downloadReplay(detail.data, attempt)}
          />
        ))}
    </>
  );
}

export function ReplayConsole({ me }: { me: Me }) {
  const allowed = me.capabilities.some((value) =>
    ["replay.full", "replay.evidence"].includes(value),
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">GOVERNANCE · SYNTHETIC DATA ONLY</span>
          <h1>Decision Replay</h1>
          <p>
            Trace a financial result to the exact inputs, versions, event stream
            and human review.
          </p>
        </div>
      </div>
      {allowed ? (
        <EvidenceWorkspace key={me.id} me={me} />
      ) : (
        <section className="panel">
          <h2>Decision evidence is restricted</h2>
          <p>
            Your current role cannot retrieve financial replay inputs or
            personnel evidence. Each API checks object, action and field
            authority.
          </p>
        </section>
      )}
      {me.capabilities.includes("audit.read") &&
        me.grants.some(
          (grant) =>
            grant.scope === "*" &&
            ["hr", "finance", "auditor"].includes(grant.role),
        ) && <AuditExplorer key={`audit-${me.id}`} me={me} />}
    </>
  );
}
