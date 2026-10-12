"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, Plus, ShieldCheck } from "lucide-react";
import type { Me } from "@/lib/api";
import { getBatch, getBatches, type Batch } from "@/lib/ingestion";
import { Failure } from "./ingestion-shared";
import { SourceForm } from "./ingestion-source-form";
import { BatchDetail } from "./ingestion-detail";

export function IngestionConsole({ me }: { me: Me }) {
  if (!me.capabilities.includes("ingestion.write"))
    return (
      <>
        <div className="page-heading">
          <div>
            <div className="eyebrow">SOURCE GOVERNANCE</div>
            <h1>Data Quality.</h1>
          </div>
        </div>
        <section className="panel empty-state">
          <ShieldCheck size={28} />
          <h2>Source stewardship access required</h2>
          <p>
            Your current role cannot import, preview, or download workforce
            source batches. Contact an HR administrator for access.
          </p>
        </section>
      </>
    );
  return <StewardConsole me={me} />;
}
function StewardConsole({ me }: { me: Me }) {
  const cache = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [form, setForm] = useState<{ parent: Batch | null } | null>(null);
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const batches = useQuery({
    queryKey: ["batches", me.id, cursors.at(-1)],
    queryFn: () => getBatches(cursors.at(-1)),
    refetchInterval: 5000,
  });
  const selectedId = selected || batches.data?.items[0]?.id;
  const selectedSource = useQuery({
    queryKey: ["batch", me.id, selectedId],
    queryFn: () => getBatch(selectedId!),
    enabled: !!selectedId,
  });
  function registered(batch: Batch) {
    cache.setQueryData(["batch", me.id, batch.id], batch);
    void cache.invalidateQueries({ queryKey: ["batches", me.id] });
    setSelected(batch.id);
    setForm(null);
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">SOURCE GOVERNANCE</div>
          <h1>Data Quality.</h1>
          <p>Validate every source. Preserve every revision.</p>
        </div>
        <button
          className="button primary"
          onClick={() => setForm({ parent: null })}
        >
          <Plus size={17} /> Import workforce
        </button>
      </div>
      <div className="scope-strip">
        <ShieldCheck size={16} />
        <span>Synthetic sources only · Zero-reject publication policy</span>
        <span className="strip-end">Private source storage</span>
      </div>
      {form && (
        <SourceForm
          key={form.parent?.id || "new"}
          me={me}
          parent={form.parent}
          onRegistered={registered}
          onClose={() => setForm(null)}
        />
      )}
      <div className="ingestion-grid">
        <section className="panel batch-list" aria-label="Registered sources">
          <div className="batch-list-heading">
            <span className="eyebrow">SOURCE REGISTER</span>
            <h2>Workforce batches</h2>
            <p>Validation and publication status</p>
          </div>
          {batches.isPending ? (
            <p className="source-message" role="status">
              Loading sources…
            </p>
          ) : batches.error ? (
            <Failure error={batches.error} retry={() => batches.refetch()} />
          ) : !batches.data.items.length ? (
            <div className="source-message">
              <Database size={24} />
              <h3>No sources registered yet</h3>
              <p>
                Import your first synthetic workforce file to start validation.
              </p>
              <button
                className="button"
                onClick={() => setForm({ parent: null })}
              >
                Choose a source file
              </button>
            </div>
          ) : (
            <>
              <ul>
                {batches.data.items.map((item) => {
                  const batch =
                    selectedSource.data?.id === item.id
                      ? selectedSource.data
                      : item;
                  return (
                    <li key={batch.id}>
                      <button
                        className={`batch-choice ${selectedId === batch.id ? "selected" : ""}`}
                        aria-pressed={selectedId === batch.id}
                        onClick={() => {
                          setSelected(batch.id);
                          setForm(null);
                        }}
                      >
                        <span className="batch-name">{batch.title}</span>
                        <span
                          className={`batch-status ${batch.state.toLowerCase()}`}
                        >
                          {batch.state}
                        </span>
                        <span>
                          {batch.source_watermark} · Revision {batch.version}
                        </span>
                        <span>
                          {batch.total_count === null
                            ? "Awaiting validation"
                            : `${batch.accepted_count.toLocaleString("en-US")} accepted · ${batch.rejected_count.toLocaleString("en-US")} rejected`}
                        </span>
                        {batch.failed_job && (
                          <strong>Worker action required</strong>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
              <div className="pagination">
                <button
                  className="button"
                  disabled={cursors.length === 1}
                  onClick={() =>
                    setCursors((previous) => previous.slice(0, -1))
                  }
                >
                  Previous sources
                </button>
                <button
                  className="button"
                  disabled={!batches.data.next_cursor}
                  onClick={() =>
                    setCursors((previous) => [
                      ...previous,
                      batches.data.next_cursor || undefined,
                    ])
                  }
                >
                  Next sources
                </button>
              </div>
            </>
          )}
        </section>
        {selectedId ? (
          <BatchDetail
            key={selectedId}
            id={selectedId}
            me={me}
            onCorrect={(batch) => {
              setForm({ parent: batch });
              window.scrollTo({ top: 0, behavior: "smooth" });
            }}
          />
        ) : (
          <section className="panel source-welcome">
            <Database size={32} />
            <span className="eyebrow">A TRUSTED WORKFORCE SOURCE</span>
            <h2>From source file to a sealed snapshot.</h2>
            <ol>
              <li>
                <strong>Import</strong>
                <span>
                  Choose a CSV or JSON source and pin its base snapshot.
                </span>
              </li>
              <li>
                <strong>Validate</strong>
                <span>
                  Inspect accepted rows and resolve quarantined errors in a
                  corrected file.
                </span>
              </li>
              <li>
                <strong>Publish</strong>
                <span>
                  Confirm a valid source. An immutable snapshot and its
                  provenance are retained.
                </span>
              </li>
            </ol>
          </section>
        )}
      </div>
    </>
  );
}
