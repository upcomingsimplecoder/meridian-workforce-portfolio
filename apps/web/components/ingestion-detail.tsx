"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Check,
  Download,
  FileCheck2,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import type { Me } from "@/lib/api";
import {
  canPublish,
  downloadSource,
  getBatch,
  publishSource,
  retrySource,
  type ArtifactKind,
  type Batch,
} from "@/lib/ingestion";
import { Failure } from "./ingestion-shared";
import { ValidationTables } from "./ingestion-tables";

export function BatchDetail({
  id,
  me,
  onCorrect,
}: {
  id: string;
  me: Me;
  onCorrect: (batch: Batch) => void;
}) {
  const batch = useQuery({
    queryKey: ["batch", me.id, id],
    queryFn: () => getBatch(id),
    refetchInterval: (query) => (query.state.data?.pending_job ? 1000 : false),
  });
  return (
    <div className="batch-detail">
      {batch.isPending ? (
        <section className="panel source-message" role="status">
          Loading source provenance…
        </section>
      ) : batch.error ? (
        <Failure error={batch.error} retry={() => batch.refetch()} />
      ) : (
        <BatchContent me={me} batch={batch.data} onCorrect={onCorrect} />
      )}
    </div>
  );
}
function BatchContent({
  me,
  batch,
  onCorrect,
}: {
  me: Me;
  batch: Batch;
  onCorrect: (batch: Batch) => void;
}) {
  const cache = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const [reason, setReason] = useState("");
  const [downloaded, setDownloaded] = useState("");
  const publish = useMutation({
    mutationFn: (key: string) => publishSource(me, batch, key),
    onSuccess: (next) => {
      cache.setQueryData(["batch", me.id, batch.id], next);
      void cache.invalidateQueries({ queryKey: ["batches", me.id] });
      void cache.invalidateQueries({ queryKey: ["snapshots", me.id] });
      setConfirm(false);
    },
    onError: () => {
      void cache.invalidateQueries({ queryKey: ["batch", me.id, batch.id] });
    },
  });
  const retry = useMutation({
    mutationFn: () => retrySource(me, batch, reason, crypto.randomUUID()),
    onSuccess: (next) => {
      cache.setQueryData(["batch", me.id, batch.id], next);
      void cache.invalidateQueries({ queryKey: ["batches", me.id] });
      setReason("");
    },
  });
  const download = useMutation({
    mutationFn: (kind: ArtifactKind) => downloadSource(me, batch, kind),
    onSuccess: (_, kind) =>
      setDownloaded(
        kind === "errors"
          ? "Redacted error report downloaded."
          : "Publication manifest downloaded.",
      ),
  });
  const validated = batch.validated_at !== null;
  const published = batch.state === "PUBLISHED";
  return (
    <>
      <section className="panel source-overview" aria-label="Selected source">
        <div className="panel-heading">
          <div>
            <div className="eyebrow">
              SOURCE BATCH · REVISION {batch.version}
            </div>
            <h2>{batch.title}</h2>
          </div>
          <span
            className={`batch-status ${batch.state.toLowerCase()}`}
            data-testid="batch-state"
          >
            {batch.state}
          </span>
        </div>
        <ol className="source-stages" aria-label="Source progress">
          <li className="complete">
            <Check size={17} />
            <span>
              Received<small>Private source retained</small>
            </span>
          </li>
          <li className={validated ? "complete" : ""}>
            <ShieldCheck size={17} />
            <span>
              Validated
              <small>
                {validated
                  ? `${batch.rejected_count} rejected rows`
                  : "Worker queued"}
              </small>
            </span>
          </li>
          <li className={published ? "complete" : ""}>
            <LockKeyhole size={17} />
            <span>
              Published
              <small>
                {published ? "Snapshot sealed" : "Confirmation required"}
              </small>
            </span>
          </li>
        </ol>
        <div className="source-counts" aria-label="Validation counts">
          <div>
            <span>Input records</span>
            <strong data-testid="input-count">
              {batch.total_count === null
                ? "Pending"
                : batch.total_count.toLocaleString("en-US")}
            </strong>
          </div>
          <div>
            <span>Accepted</span>
            <strong data-testid="accepted-count">
              {validated
                ? batch.accepted_count.toLocaleString("en-US")
                : "Pending"}
            </strong>
          </div>
          <div className={batch.rejected_count ? "count-warning" : ""}>
            <span>Quarantined</span>
            <strong data-testid="rejected-count">
              {validated
                ? batch.rejected_count.toLocaleString("en-US")
                : "Pending"}
            </strong>
          </div>
        </div>
        {batch.pending_job && (
          <div className="worker-notice" role="status">
            <RefreshCw size={17} />
            <p>
              {published
                ? "Snapshot published. The worker is preparing its manifest."
                : "Source registered. The worker will validate the file; this view updates when it finishes."}{" "}
              If the worker is offline, the durable job remains queued.
            </p>
          </div>
        )}
        {batch.failed_job && (
          <div className="error-box" role="alert">
            <h3>Worker action required</h3>
            <p>
              The job exhausted its retries. Restore worker and private storage
              availability, then record a reason to retry. Existing snapshots
              remain sealed.
            </p>
            <form
              className="retry-form"
              onSubmit={(event) => {
                event.preventDefault();
                retry.mutate();
              }}
            >
              <label>
                Retry reason
                <input
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  minLength={8}
                  maxLength={200}
                  required
                />
              </label>
              <button
                className="button"
                disabled={retry.isPending}
                type="submit"
              >
                Retry failed job
              </button>
            </form>
          </div>
        )}
        {batch.state === "QUARANTINED" && (
          <div className="quarantine-notice">
            <FileCheck2 size={20} />
            <div>
              <h3>Correction required before publication</h3>
              <p>
                Resolve all errors in a corrected source file. Nothing from this
                batch has been published.
              </p>
            </div>
            <button className="button" onClick={() => onCorrect(batch)}>
              Upload correction
            </button>
          </div>
        )}
        {canPublish(batch) && (
          <div className="publish-notice">
            <ShieldCheck size={20} />
            <div>
              <h3>Validation complete. Ready for review.</h3>
              <p>
                Every input row passed. Review the accepted preview and source
                lineage, then publish a new snapshot.
              </p>
            </div>
            <button
              className="button primary"
              onClick={(event) => {
                event.currentTarget.focus();
                setConfirm(true);
              }}
            >
              Review publication
            </button>
          </div>
        )}
        {published && (
          <div className="publish-notice">
            <LockKeyhole size={20} />
            <div>
              <h3>Immutable snapshot published</h3>
              <p>Earlier snapshots and effective histories remain available.</p>
            </div>
            <Link
              className="button"
              href={`/workforce?snapshot_id=${batch.snapshot_id}&as_of=${batch.source_watermark}`}
            >
              Explore snapshot <ArrowRight size={15} />
            </Link>
          </div>
        )}
        <div className="source-downloads">
          <button
            className="button"
            disabled={!validated || download.isPending}
            onClick={() => download.mutate("errors")}
          >
            <Download size={15} /> Download redacted errors
          </button>
          <button
            className="button"
            disabled={!batch.manifest_ready || download.isPending}
            onClick={() => download.mutate("manifest")}
          >
            <Download size={15} /> Download manifest
          </button>
        </div>
        {downloaded && (
          <p className="source-operation" role="status">
            {downloaded}
          </p>
        )}
        {[publish.error, retry.error, download.error].map(
          (error, index) => error && <Failure key={index} error={error} />,
        )}
        <details className="source-lineage">
          <summary>Source lineage and immutable evidence</summary>
          <dl className="evidence-list">
            <dt>Batch</dt>
            <dd>{batch.id}</dd>
            <dt>Base snapshot</dt>
            <dd>{batch.base_snapshot_id}</dd>
            <dt>Source watermark</dt>
            <dd>{batch.source_watermark}</dd>
            <dt>Source SHA-256</dt>
            <dd>{batch.source_sha256}</dd>
            <dt>Parser version</dt>
            <dd>{batch.parser_version}</dd>
            <dt>Corrects batch</dt>
            <dd>{batch.supersedes_batch_id || "Original import"}</dd>
            <dt>Received</dt>
            <dd>{batch.received_at}</dd>
            <dt>Validated</dt>
            <dd>{batch.validated_at || "Pending"}</dd>
            <dt>Published</dt>
            <dd>{batch.published_at || "Not published"}</dd>
            <dt>New snapshot</dt>
            <dd>{batch.snapshot_id || "Not published"}</dd>
          </dl>
        </details>
      </section>
      {validated && (
        <ValidationTables
          id={batch.id}
          principal={me.id}
          revision={batch.version}
        />
      )}
      {confirm && (
        <PublishConfirmation
          batch={batch}
          pending={publish.isPending}
          error={publish.error}
          onClose={() => setConfirm(false)}
          onConfirm={(key) => publish.mutate(key)}
        />
      )}
    </>
  );
}
export function PublishConfirmation({
  batch,
  pending,
  error,
  onClose,
  onConfirm,
}: {
  batch: Batch;
  pending: boolean;
  error: Error | null;
  onClose: () => void;
  onConfirm: (key: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [key] = useState(() => crypto.randomUUID());
  useEffect(() => {
    const element = dialog.current;
    const prior = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (prior instanceof HTMLElement) prior.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="publish-dialog"
      aria-labelledby="publish-title"
      onCancel={(event) => {
        if (pending) event.preventDefault();
        else onClose();
      }}
    >
      <div className="eyebrow">PUBLISH A WORKFORCE SNAPSHOT</div>
      <h2 id="publish-title">Confirm source publication</h2>
      <p>
        <strong>{batch.title}</strong> contains{" "}
        {batch.accepted_count.toLocaleString("en-US")} accepted rows and{" "}
        {batch.rejected_count} rejected rows. Publication applies its effective
        intervals to the pinned base and creates a new immutable snapshot.
      </p>
      <dl className="evidence-list">
        <dt>Source revision</dt>
        <dd>{batch.version}</dd>
        <dt>Watermark</dt>
        <dd>{batch.source_watermark}</dd>
        <dt>Base snapshot</dt>
        <dd>{batch.base_snapshot_id}</dd>
      </dl>
      <label className="publication-ack">
        <input
          type="checkbox"
          checked={acknowledged}
          onChange={(event) => setAcknowledged(event.target.checked)}
          disabled={pending}
        />
        I reviewed the valid source and confirm zero-reject publication.
      </label>
      {error && <Failure error={error} />}
      <div className="dialog-actions">
        <button className="button" onClick={onClose} disabled={pending}>
          Cancel publication
        </button>
        <button
          className="button primary"
          disabled={!acknowledged || !canPublish(batch) || pending}
          onClick={() => onConfirm(key)}
        >
          {pending ? "Publishing…" : "Publish immutable snapshot"}
        </button>
      </div>
    </dialog>
  );
}
