"use client";
import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { X, History, ShieldCheck } from "lucide-react";
import {
  formatMoney,
  getHistory,
  getPerson,
  type HistoricalFilters,
} from "@/lib/workforce";

export function PersonDetail({
  id,
  principal,
  filters,
  onClose,
}: {
  id: string;
  principal: string;
  filters: HistoricalFilters;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const person = useQuery({
    queryKey: ["person", principal, id, filters],
    queryFn: () => getPerson(id, filters),
  });
  const history = useQuery({
    queryKey: ["history", principal, id, filters],
    queryFn: () => getHistory(id, filters),
  });
  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="person-dialog"
      aria-labelledby="person-title"
      onCancel={onClose}
    >
      <div className="detail-header">
        <div>
          <div className="eyebrow">SYNTHETIC WORKFORCE RECORD</div>
          <h2 id="person-title">
            {person.data?.display_name || "Person details"}
          </h2>
          <p>
            {id} · Effective {filters.as_of}
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Close person details"
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </div>
      {person.isPending ? (
        <p role="status">Loading permitted record…</p>
      ) : person.error ? (
        <div className="error-box" role="alert">
          <p>{person.error.message}</p>
          <button className="button" onClick={() => person.refetch()}>
            Retry person
          </button>
        </div>
      ) : (
        <>
          <div className="detail-scope">
            <ShieldCheck size={16} /> Fields reflect your current access
          </div>
          <dl className="person-facts">
            <div>
              <dt>Department</dt>
              <dd>{person.data.org_name}</dd>
            </div>
            <div>
              <dt>Region</dt>
              <dd>{person.data.region}</dd>
            </div>
            <div>
              <dt>Position</dt>
              <dd>{person.data.position_id}</dd>
            </div>
            <div>
              <dt>Job family / grade</dt>
              <dd>
                {person.data.job_family} / {person.data.grade}
              </dd>
            </div>
            <div>
              <dt>Reports to</dt>
              <dd>{person.data.manager_id || "No reporting parent"}</dd>
            </div>
            <div>
              <dt>FTE</dt>
              <dd>{person.data.fte}</dd>
            </div>
            {"annual_pay" in person.data && (
              <div className="pay-fact">
                <dt>Annual base pay · original currency</dt>
                <dd>{formatMoney(person.data.annual_pay)}</dd>
              </div>
            )}
          </dl>
          <h3 className="timeline-title">
            <History size={17} /> Effective history
          </h3>
          {history.isPending ? (
            <p role="status">Loading history…</p>
          ) : history.error ? (
            <div className="error-box" role="alert">
              <p>{history.error.message}</p>
              <button className="button" onClick={() => history.refetch()}>
                Retry history
              </button>
            </div>
          ) : (
            <>
              <p className="detail-note">
                Revisions from the selected immutable snapshot, visible through{" "}
                {filters.as_of}. Reporting scope is checked for each interval.
              </p>
              <ol className="history-list">
                {history.data.items.map((row) => (
                  <li key={row.version_id}>
                    <span className="history-date">
                      {row.valid_from} → {row.valid_to || "Open interval"}
                    </span>
                    <strong>{row.org_name}</strong>
                    <p>
                      {row.manager_id
                        ? `Reports to ${row.manager_id}`
                        : "No reporting parent"}{" "}
                      · {row.fte} FTE
                    </p>
                    {"annual_pay" in row && (
                      <p className="numeric">{formatMoney(row.annual_pay)}</p>
                    )}
                    <details>
                      <summary>Source evidence</summary>
                      <dl className="evidence-list">
                        <dt>Version</dt>
                        <dd>{row.version_id}</dd>
                        <dt>Source batch</dt>
                        <dd>{row.source_batch_id}</dd>
                        <dt>Recorded</dt>
                        <dd>{row.recorded_at}</dd>
                      </dl>
                    </details>
                  </li>
                ))}
              </ol>
              {history.data.redacted && (
                <p className="detail-note">
                  Some history or fields are restricted by your current
                  permissions.
                </p>
              )}
            </>
          )}
          <div className="detail-provenance">
            <span>PINNED SNAPSHOT</span>
            <strong>{filters.snapshot_id}</strong>
          </div>
        </>
      )}
    </dialog>
  );
}
