"use client";
import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  getTalentPacket,
  talentPacketCommand,
  type TalentPacket,
  type TalentPacketCommand,
  type TalentReview,
} from "@/lib/talent";

export function TalentPacketEditor({
  me,
  initial,
  onAcknowledged,
  onClose,
}: {
  me: Me;
  initial: TalentPacket;
  onAcknowledged: (packet: TalentPacket) => void;
  onClose: () => void;
}) {
  const [packet, setPacket] = useState(initial);
  const [draft, setDraft] = useState<TalentReview>(initial.review);
  const [reason, setReason] = useState("");
  const [auto, setAuto] = useState(true);
  const [signed, setSigned] = useState(false);
  const [comment, setComment] = useState("");
  const [notice, setNotice] = useState(
    `Loaded server revision ${initial.revision}.`,
  );
  const [historyError, setHistoryError] = useState("");
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [decisions, setDecisions] = useState(initial.decisions);
  const [decisionCursor, setDecisionCursor] = useState(
    initial.decisions_next_version,
  );
  const [comments, setComments] = useState(initial.comments);
  const [commentCursor, setCommentCursor] = useState(
    initial.comments_next_cursor,
  );
  const dirty = JSON.stringify(draft) !== JSON.stringify(packet.review);
  const validDraft = (draft.evidence || []).every(
    (ref) =>
      ref.observation.trim().length >= 10 &&
      ref.source_version_id === packet.source_version_id,
  );
  const editable = packet.can_edit || packet.can_calibrate;
  const command = useMutation({
    mutationFn: (request: TalentPacketCommand) =>
      talentPacketCommand(me, packet, request, crypto.randomUUID()),
    onSuccess: (data) => {
      setPacket(data);
      setDraft(data.review);
      setSigned(false);
      setComment("");
      setDecisions(data.decisions);
      setDecisionCursor(data.decisions_next_version);
      setComments(data.comments);
      setCommentCursor(data.comments_next_cursor);
      setNotice(
        `Saved on server · revision ${data.revision} · version ${data.version}.`,
      );
      onAcknowledged(data);
    },
    onError: () => {
      setAuto(false);
      setNotice("Draft retained. Resolve the error before saving again.");
    },
  });
  const { mutate, isPending } = command;
  useEffect(() => {
    if (
      !dirty ||
      !packet.can_edit ||
      !auto ||
      reason.trim().length < 3 ||
      !validDraft ||
      isPending
    )
      return;
    const timer = setTimeout(
      () => mutate({ action: "review", body: { review: draft, reason } }),
      800,
    );
    return () => clearTimeout(timer);
  }, [
    dirty,
    packet.can_edit,
    draft,
    reason,
    auto,
    validDraft,
    isPending,
    mutate,
  ]);

  async function reload(keepDraft: boolean, revision?: number) {
    if (isPending || loadingHistory) return;
    setLoadingHistory(true);
    setHistoryError("");
    try {
      const data = await getTalentPacket(packet.id, { revision });
      setPacket(data);
      if (!keepDraft) setDraft(data.review);
      setDecisions(data.decisions);
      setDecisionCursor(data.decisions_next_version);
      setComments(data.comments);
      setCommentCursor(data.comments_next_cursor);
      setSigned(false);
      if (keepDraft) {
        setAuto(false);
        setNotice(
          "Current server version loaded. Retained draft requires an explicit save.",
        );
      } else {
        setNotice(`Loaded server revision ${data.revision}.`);
        command.reset();
      }
    } catch (error) {
      setHistoryError(
        error instanceof Error
          ? error.message
          : "Revision could not be loaded.",
      );
    } finally {
      setLoadingHistory(false);
    }
  }
  async function more(kind: "decisions" | "comments") {
    setLoadingHistory(true);
    setHistoryError("");
    try {
      const data = await getTalentPacket(packet.id, {
        revision: packet.revision,
        ...(kind === "decisions"
          ? { decision_cursor: decisionCursor ?? undefined }
          : { comment_cursor: commentCursor ?? undefined }),
      });
      if (kind === "decisions") {
        setDecisions((rows) => [...rows, ...data.decisions]);
        setDecisionCursor(data.decisions_next_version);
      } else {
        setComments((rows) => [...rows, ...data.comments]);
        setCommentCursor(data.comments_next_cursor);
      }
    } catch (error) {
      setHistoryError(
        error instanceof Error ? error.message : "History could not be loaded.",
      );
    } finally {
      setLoadingHistory(false);
    }
  }
  function action(name: string) {
    const body = { reason };
    if (name === "approve" || name === "ack-calibration")
      mutate({ action: name, body: { ...body, acknowledgement: true } });
    else if (
      name === "submit" ||
      name === "start-calibration" ||
      name === "return"
    )
      mutate({ action: name, body });
  }
  const labels: Record<string, string> = {
    submit: "Submit human review",
    "start-calibration": "Begin calibration",
    "ack-calibration": "Acknowledge calibration",
    approve: "Approve reviewed packet",
    return: "Return for revision",
  };
  const currentSignatures = packet.signatures.filter(
    (r) =>
      r.revision_id === packet.revision_id &&
      ["APPROVE", "ACK_CALIBRATION"].includes(r.action) &&
      r.acknowledgement,
  );
  return (
    <section className="panel talent-packet" aria-labelledby="packet-heading">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">HUMAN REVIEW PACKET</div>
          <h2 id="packet-heading">{packet.display_name}</h2>
          <p>
            {packet.employee_id} · {packet.state} · revision {packet.revision}{" "}
            of {packet.current_revision} · server version {packet.version}
          </p>
        </div>
        <button
          className="button"
          disabled={dirty || isPending}
          onClick={onClose}
        >
          Close packet
        </button>
      </div>
      <div className="talent-packet-body">
        <div className="talent-context">
          <dl>
            <dt>Source context</dt>
            <dd>
              {String(packet.context.job_family)} · {packet.context.grade} ·{" "}
              {String(packet.context.org_id)}
            </dd>
            <dt>Author → calibrator → HR</dt>
            <dd>
              {packet.author_id} → {packet.calibrator_id} →{" "}
              {packet.hr_signer_id}
            </dd>
            <dt>Source version</dt>
            <dd>
              <code>{packet.source_version_id}</code>
            </dd>
            <dt>Context SHA-256</dt>
            <dd>
              <code>{packet.context_sha256}</code>
            </dd>
          </dl>
          <p className="muted">
            Workforce context locates a synthetic observation. It does not
            independently prove performance.
          </p>
        </div>
        <div className="talent-editor-tools">
          <label>
            View immutable revision
            <select
              aria-label="View immutable revision"
              value={packet.revision}
              disabled={dirty || isPending || loadingHistory}
              onChange={(e) => void reload(false, Number(e.target.value))}
            >
              {packet.revisions.map((r) => (
                <option key={r.id} value={r.number}>
                  Revision {r.number} · {r.kind} · {r.author_id}
                </option>
              ))}
            </select>
          </label>
          <button
            className="button"
            disabled={isPending || loadingHistory}
            onClick={() => void reload(dirty)}
          >
            Reload current revision {dirty ? "(keep draft)" : ""}
          </button>
        </div>
        {historyError && (
          <p className="error-box" role="alert">
            {historyError}
          </p>
        )}
        {packet.revision !== packet.current_revision && (
          <p className="talent-historical">
            Historical immutable revision. New signatures and edits bind the
            current revision only.
          </p>
        )}
        {packet.state === "LOCKED" && (
          <p className="talent-historical">
            Locked packet. HR can reopen it as a new revision; this signed
            history stays immutable.
          </p>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            mutate({
              action: packet.can_calibrate ? "calibration" : "review",
              body: { review: draft, reason },
            });
          }}
        >
          <fieldset disabled={isPending}>
            <div className="talent-narratives">
              {(
                ["accomplishments", "collaboration", "development"] as const
              ).map((key) => (
                <label key={key}>
                  {key[0].toUpperCase() + key.slice(1)}
                  {editable || dirty ? (
                    <textarea
                      aria-label={key[0].toUpperCase() + key.slice(1)}
                      rows={4}
                      maxLength={2000}
                      disabled={!editable}
                      value={draft[key] || ""}
                      onChange={(e) =>
                        setDraft((r) => ({ ...r, [key]: e.target.value }))
                      }
                    />
                  ) : (
                    <p className="talent-narrative-read">
                      {packet.review[key] || "No narrative recorded."}
                    </p>
                  )}
                </label>
              ))}
            </div>
            <h3>Observable evidence references</h3>
            {(draft.evidence || []).map((ref, i) => (
              <div className="talent-reference" key={i}>
                <code>{ref.source_version_id}</code>
                {editable || dirty ? (
                  <label>
                    Synthetic observation {i + 1}
                    <textarea
                      aria-label={`Synthetic observation ${i + 1}`}
                      rows={2}
                      maxLength={240}
                      disabled={!editable}
                      value={ref.observation}
                      onChange={(e) =>
                        setDraft((r) => ({
                          ...r,
                          evidence: (r.evidence || []).map((v, index) =>
                            index === i
                              ? { ...v, observation: e.target.value }
                              : v,
                          ),
                        }))
                      }
                    />
                  </label>
                ) : (
                  <p>{ref.observation}</p>
                )}
                {editable && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      setDraft((r) => ({
                        ...r,
                        evidence: (r.evidence || []).filter(
                          (_, index) => index !== i,
                        ),
                      }))
                    }
                  >
                    Remove reference {i + 1}
                  </button>
                )}
              </div>
            ))}
            {!draft.evidence?.length && (
              <p className="muted">No evidence references recorded.</p>
            )}
            {editable && (
              <button
                type="button"
                className="button"
                disabled={(draft.evidence || []).length >= 10}
                onClick={() =>
                  setDraft((r) => ({
                    ...r,
                    evidence: [
                      ...(r.evidence || []),
                      {
                        kind: "WORKFORCE_CONTEXT",
                        source_version_id: packet.source_version_id,
                        observation: "",
                      },
                    ],
                  }))
                }
              >
                Add context reference
              </button>
            )}
            <label className="talent-reason">
              Packet action reason
              <textarea
                required
                minLength={3}
                maxLength={240}
                rows={2}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            {packet.can_edit && (
              <label className="talent-checkbox">
                <input
                  type="checkbox"
                  tabIndex={0}
                  checked={auto}
                  onChange={(e) => setAuto(e.target.checked)}
                />{" "}
                Autosave author drafts after a pause
              </label>
            )}
            {editable && (
              <button
                className="button primary"
                type="submit"
                disabled={!dirty || reason.trim().length < 3 || !validDraft}
              >
                {packet.can_calibrate
                  ? "Save calibration revision"
                  : "Save retained draft"}
              </button>
            )}
          </fieldset>
        </form>
        <p className="talent-save-status" role="status" aria-live="polite">
          {isPending
            ? "Saving to server…"
            : dirty
              ? `Unsaved draft · ${auto && packet.can_edit && reason.trim().length >= 3 && validDraft ? "autosave pending" : "explicit save or valid reason/reference required"}`
              : notice}
        </p>
        {command.error && (
          <p role="alert" className="error-box">
            {command.error.message} Your draft remains in this page; reload the
            current version before an explicit save.
          </p>
        )}
        <div className="talent-checklist">
          <h3>Submission checklist</h3>
          {packet.checklist.length ? (
            <ul>
              {packet.checklist.map((code) => (
                <li key={code}>{code.replaceAll("_", " ").toLowerCase()}</li>
              ))}
            </ul>
          ) : (
            <p>
              All required narrative sections and context references are saved.
            </p>
          )}
        </div>
        <section className="talent-signatures">
          <h3>Signatures on this revision: {currentSignatures.length}</h3>
          {currentSignatures.map((row) => (
            <p key={row.id}>
              {row.action === "APPROVE"
                ? "HR review"
                : "Calibration participation"}{" "}
              · {row.actor_id} · {row.reason}
            </p>
          ))}
        </section>
        {packet.allowed_actions.some(
          (a) => a === "approve" || a === "ack-calibration",
        ) && (
          <label className="talent-checkbox">
            <input
              type="checkbox"
              tabIndex={0}
              checked={signed}
              disabled={isPending || dirty}
              onChange={(e) => setSigned(e.target.checked)}
            />{" "}
            I reviewed this exact revision and acknowledge my human review
            responsibility.
          </label>
        )}
        <div className="talent-actions">
          {packet.allowed_actions
            .filter((a) => a !== "comment")
            .map((name) => (
              <button
                key={name}
                tabIndex={0}
                className={name === "return" ? "button" : "button primary"}
                disabled={
                  isPending ||
                  dirty ||
                  reason.trim().length < 3 ||
                  (["approve", "ack-calibration"].includes(name) && !signed)
                }
                onClick={() => action(name)}
              >
                {labels[name]}
              </button>
            ))}
        </div>
        {packet.allowed_actions.includes("comment") && (
          <form
            className="talent-comment-form"
            onSubmit={(e) => {
              e.preventDefault();
              mutate({ action: "comments", body: { body: comment, reason } });
            }}
          >
            <label>
              Scoped packet comment
              <textarea
                value={comment}
                minLength={3}
                maxLength={500}
                required
                rows={2}
                disabled={isPending}
                onChange={(e) => setComment(e.target.value)}
              />
            </label>
            <button
              className="button"
              disabled={
                isPending ||
                dirty ||
                reason.trim().length < 3 ||
                comment.trim().length < 3
              }
            >
              Record comment
            </button>
          </form>
        )}
        <details className="talent-history" open>
          <summary>Calibration changes · {packet.calibrations.length}</summary>
          {packet.calibrations.map((row) => (
            <div className="talent-history-row" key={row.revision_id}>
              <strong>
                {row.actor_id} · {row.reason}
              </strong>
              <dl>
                <dt>Before SHA-256</dt>
                <dd>
                  <code>{row.before_sha256}</code>
                </dd>
                <dt>After SHA-256</dt>
                <dd>
                  <code>{row.after_sha256}</code>
                </dd>
              </dl>
            </div>
          ))}
        </details>
        <details className="talent-history">
          <summary>Human decision history</summary>
          <ol>
            {decisions.map((row) => (
              <li key={row.id}>
                <strong>
                  {row.action} · {row.actor_id}
                </strong>
                <p>{row.reason}</p>
                <small>
                  {new Date(row.created_at).toLocaleString()} · request{" "}
                  {row.request_id} · revision {row.revision_id}
                </small>
              </li>
            ))}
          </ol>
          {decisionCursor !== null && (
            <button
              className="button"
              disabled={loadingHistory}
              onClick={() => void more("decisions")}
            >
              More decisions
            </button>
          )}
        </details>
        <details className="talent-history">
          <summary>Scoped comments</summary>
          {comments.map((row) => (
            <div key={row.id} className="talent-history-row">
              <strong>{row.author_id}</strong>
              <p>{row.body}</p>
              <code>{row.sha256}</code>
            </div>
          ))}
          {commentCursor !== null && (
            <button
              className="button"
              disabled={loadingHistory}
              onClick={() => void more("comments")}
            >
              More comments
            </button>
          )}
        </details>
        <p className="talent-fingerprint">
          Revision SHA-256 <code>{packet.revision_sha256}</code>
        </p>
      </div>
    </section>
  );
}
