"use client";
import { useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { FileUp, X } from "lucide-react";
import type { Me } from "@/lib/api";
import { registerSource, sourceMedia, type Batch } from "@/lib/ingestion";
import { getSnapshots, type Snapshot } from "@/lib/workforce";
import { Failure } from "./ingestion-shared";
import { SnapshotSearch } from "./snapshot-search";

export function SourceForm({
  me,
  parent,
  onRegistered,
  onClose,
}: {
  me: Me;
  parent: Batch | null;
  onRegistered: (batch: Batch) => void;
  onClose: () => void;
}) {
  const [sourceSearch, setSourceSearch] = useState("");
  const sources = useQuery({
    queryKey: ["snapshots", me.id, sourceSearch, parent?.base_snapshot_id],
    queryFn: () => getSnapshots(sourceSearch, parent?.base_snapshot_id),
    placeholderData: keepPreviousData,
  });
  return (
    <section
      className="panel source-form-panel"
      aria-label={parent ? "Correct quarantined source" : "Import a source"}
    >
      <div className="panel-heading">
        <div>
          <span className="eyebrow">
            {parent ? "CORRECTION REVISION" : "NEW WORKFORCE SOURCE"}
          </span>
          <h2>
            {parent
              ? "Resolve the quarantined source"
              : "Import a synthetic workforce file"}
          </h2>
        </div>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="Close source form"
        >
          <X size={20} />
        </button>
      </div>
      {!parent && (
        <SnapshotSearch
          onSearch={setSourceSearch}
          pending={sources.isFetching}
        />
      )}
      {sources.isPending ? (
        <p role="status">Loading source snapshots…</p>
      ) : sources.error ? (
        <Failure error={sources.error} retry={() => sources.refetch()} />
      ) : (
        <UploadForm
          me={me}
          parent={parent}
          snapshots={sources.data}
          onRegistered={onRegistered}
        />
      )}
    </section>
  );
}
function UploadForm({
  me,
  parent,
  snapshots,
  onRegistered,
}: {
  me: Me;
  parent: Batch | null;
  snapshots: Snapshot[];
  onRegistered: (batch: Batch) => void;
}) {
  const file = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState(
    parent ? `${parent.title.slice(0, 80)} · corrected` : "",
  );
  const [base, setBase] = useState(
    parent?.base_snapshot_id || snapshots[0]?.id || "",
  );
  const [watermark, setWatermark] = useState(
    parent?.source_watermark || snapshots[0]?.source_watermark || "",
  );
  const [progress, setProgress] = useState("");
  const mutation = useMutation({
    mutationFn: async () => {
      const selected = file.current?.files?.[0];
      if (!selected) throw new Error("Choose a source file before importing.");
      sourceMedia(selected);
      return registerSource(
        me,
        selected,
        {
          title: title.trim(),
          base_snapshot_id: base,
          watermark,
          schema_version: 1,
          supersedes_batch_id: parent?.id || null,
        },
        setProgress,
      );
    },
    onSuccess: onRegistered,
  });
  return (
    <form
      className="source-form"
      onSubmit={(event) => {
        event.preventDefault();
        mutation.mutate();
      }}
    >
      <p className="source-form-intro">
        {parent
          ? "Upload the corrected file. The original bytes and errors remain in the register, linked to this revision."
          : "CSV or JSON · Workforce schema v1 · Maximum 8 MiB and 10,000 records. Files must use synthetic IDs and names."}
      </p>
      {parent && (
        <p className="correction-reference">
          Corrects batch <code>{parent.id}</code>
        </p>
      )}
      <fieldset disabled={mutation.isPending}>
        <div className="source-form-fields">
          <label>
            Source title
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              required
              minLength={3}
              maxLength={100}
              placeholder="July workforce correction"
            />
          </label>
          <label>
            Base snapshot
            <select
              aria-label="Base snapshot"
              value={base}
              onChange={(event) => setBase(event.target.value)}
              disabled={!!parent}
              required
            >
              {snapshots.map((snapshot) => (
                <option key={snapshot.id} value={snapshot.id}>
                  {snapshot.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            Source watermark
            <input
              type="date"
              value={watermark}
              onChange={(event) => setWatermark(event.target.value)}
              min="1900-01-01"
              max="2200-12-31"
              required
            />
          </label>
        </div>
        <label className="source-file">
          <FileUp size={25} />
          <span>
            <strong>Workforce source file</strong>
            <small>
              Choose a synthetic CSV or JSON file from your computer.
            </small>
          </span>
          <input
            ref={file}
            type="file"
            accept=".csv,.json,text/csv,application/json"
            required
            aria-label="Workforce source file"
          />
        </label>
        <div className="source-form-actions">
          <p>
            Missing identities remain unchanged. Represent exits with an
            explicit dated EXITED record.
          </p>
          <button className="button primary" type="submit">
            {mutation.isPending ? "Importing source…" : "Upload and validate"}
          </button>
        </div>
      </fieldset>
      {mutation.isPending && (
        <p role="status" className="source-operation">
          {progress}
        </p>
      )}
      {mutation.error && <Failure error={mutation.error} />}
      <details className="source-format">
        <summary>Schema and correction guide</summary>
        <p>
          Use <code>SYN-000001</code> with <code>Synthetic Worker 000001</code>.
          Monetary values are integer minor-unit strings in their original
          currency (USD, EUR, GBP, JPY or KWD). FTE must be positive and at most
          1, with up to four decimal places. Dates use YYYY-MM-DD; the end date
          is exclusive. The watermark must cover every effective start.
        </p>
        <p>
          CSV columns: employee_id, display_name, position_id, org_id,
          manager_id, job_family, grade, region, status, annual_pay_minor,
          currency, fte, valid_from, valid_to. JSON wraps the same records in
          schema_version: 1 and records. The local fixture generator supplies
          defective and corrected examples.
        </p>
      </details>
    </form>
  );
}
