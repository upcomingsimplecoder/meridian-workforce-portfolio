"use client";
import { useState } from "react";
import Link from "next/link";
import { replayHref } from "@/lib/replay";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  compMoney,
  getProposal,
  proposalCommand,
  type Proposal,
  type ReviewRequest,
} from "@/lib/compensation";

function ReviewForm({ me, proposal }: { me: Me; proposal: Proposal }) {
  const [reason, setReason] = useState("");
  const [action, setAction] = useState<"approve" | "return">("approve");
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const client = useQueryClient();
  const step = proposal.steps.find((item) => item.active);
  const mutation = useMutation({
    mutationFn: (start: boolean) =>
      proposalCommand(
        me,
        proposal,
        start
          ? {
              action: "review",
              body: { reason, revision_id: proposal.revision_id },
            }
          : {
              action: "decisions",
              body: {
                reason,
                revision_id: proposal.revision_id,
                decision: action,
                acknowledged_exceptions: (action === "approve"
                  ? acknowledged
                  : []) as ReviewRequest["acknowledged_exceptions"],
              },
            },
        crypto.randomUUID(),
      ),
    onSuccess: async () => {
      setReason("");
      setAcknowledged([]);
      await client.invalidateQueries({ queryKey: ["comp", me.id] });
    },
  });
  if (
    !step ||
    step.assigned_id !== me.id ||
    !me.capabilities.includes(step.capability) ||
    proposal.proposer_id === me.id ||
    proposal.revision !== proposal.current_revision
  )
    return (
      <p className="fine-print">
        This revision has no current decision assigned to your account.
      </p>
    );
  const start = proposal.state === "SUBMITTED";
  return (
    <form
      className="comp-review-form"
      onSubmit={(event) => {
        event.preventDefault();
        mutation.mutate(start);
      }}
    >
      <h3>
        {start ? "Begin division review" : `${step.role} decision`} · revision{" "}
        {proposal.revision}
      </h3>
      <p>
        This action records your account, the exact saved revision and a reason.
      </p>
      <fieldset disabled={mutation.isPending}>
        <legend className="sr-only">Assigned decision</legend>
        {!start && (
          <label>
            Decision
            <select
              aria-label="Decision"
              value={action}
              onChange={(event) =>
                setAction(event.target.value as "approve" | "return")
              }
            >
              <option value="approve">Approve this revision</option>
              <option value="return">Return for manager revision</option>
            </select>
          </label>
        )}
        <label>
          Decision reason
          <textarea
            required
            minLength={3}
            maxLength={240}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={2}
          />
        </label>
        {!start && step.required_exceptions.length > 0 && (
          <fieldset>
            <legend>Explicit exception acknowledgements</legend>
            {step.required_exceptions.map((code) => (
              <label className="comp-check" key={code}>
                <input
                  type="checkbox"
                  required={action === "approve"}
                  checked={acknowledged.includes(code)}
                  onChange={(event) =>
                    setAcknowledged((items) =>
                      event.target.checked
                        ? [...items, code]
                        : items.filter((item) => item !== code),
                    )
                  }
                />
                {code.replaceAll("_", " ")}
              </label>
            ))}
          </fieldset>
        )}
        {mutation.error && (
          <p role="alert" className="error-box">
            {mutation.error.message}
          </p>
        )}
        <button className="button primary" type="submit">
          {mutation.isPending
            ? "Recording decision…"
            : start
              ? "Begin assigned review"
              : action === "approve"
                ? "Approve current revision"
                : "Return current revision"}
        </button>
      </fieldset>
    </form>
  );
}

export function CompensationReview({
  me,
  id,
  onClose,
}: {
  me: Me;
  id: string;
  onClose: () => void;
}) {
  const [revision, setRevision] = useState<number>();
  const query = useQuery({
    queryKey: ["comp", me.id, "proposal", id, revision],
    queryFn: () => getProposal(id, revision),
  });
  const proposal = query.data;
  const money = (minor: string) =>
    proposal
      ? compMoney(
          minor,
          proposal.award.currency,
          proposal.award.currency_exponent,
        )
      : "";
  return (
    <section
      className="panel comp-review"
      aria-labelledby="comp-review-heading"
    >
      <div className="panel-heading">
        <h2 id="comp-review-heading">Proposal decisions and provenance</h2>
        <button className="button" onClick={onClose}>
          Close proposal details
        </button>
      </div>
      {query.isPending ? (
        <p role="status">Loading the authorized saved proposal…</p>
      ) : query.error ? (
        <div role="alert" className="error-box">
          {query.error.message}
          <button className="button" onClick={() => query.refetch()}>
            Reload current access
          </button>
        </div>
      ) : (
        proposal && (
          <>
            <h3>
              {proposal.display_name} · {proposal.employee_id}
            </h3>
            <label>
              Saved proposal revision
              <select
                aria-label="Saved proposal revision"
                value={revision ?? "current"}
                onChange={(event) =>
                  setRevision(
                    event.target.value === "current"
                      ? undefined
                      : Number(event.target.value),
                  )
                }
              >
                <option value="current">
                  Current revision {proposal.current_revision}
                </option>
                {proposal.revisions.map((row) => (
                  <option key={row.id} value={row.number}>
                    {row.number} · {row.kind} · {row.author_id}
                  </option>
                ))}
              </select>
            </label>
            {proposal.revision !== proposal.current_revision && (
              <p role="status">
                Historical revision · read only. Current approvals apply to
                revision {proposal.current_revision}.
              </p>
            )}
            <dl className="comp-metrics">
              <div>
                <dt>Merit / annual full-time increment</dt>
                <dd>{money(proposal.award.merit_minor)}</dd>
              </div>
              <div>
                <dt>Annualized merit cost</dt>
                <dd>{money(proposal.award.annualized_merit_minor)}</dd>
              </div>
              <div>
                <dt>First-year merit cost</dt>
                <dd>{money(proposal.award.merit_first_year_minor)}</dd>
              </div>
              <div>
                <dt>Payable one-time bonus</dt>
                <dd>{money(proposal.award.bonus_payable_minor)}</dd>
              </div>
              <div>
                <dt>Total pool reservation</dt>
                <dd>
                  {me.capabilities.includes("replay.full") &&
                  proposal.verified ? (
                    <Link
                      href={replayHref("AWARD", proposal.revision_id)}
                      aria-label="Replay this compensation award"
                    >
                      {money(proposal.award.pool_charge_minor)}
                    </Link>
                  ) : (
                    money(proposal.award.pool_charge_minor)
                  )}
                </dd>
              </div>
              <div>
                <dt>Worker verification</dt>
                <dd>{proposal.verified ? "Verified" : "Pending"}</dd>
              </div>
            </dl>
            <p>
              <strong>Manager rationale:</strong>{" "}
              {proposal.comment || "Not recorded"}
            </p>
            <ol className="comp-steps">
              {proposal.steps.map((step) => (
                <li key={step.ordinal}>
                  <strong>{step.role}</strong>
                  <span>{step.assigned_id}</span>
                  <small>
                    {step.approved
                      ? "Signed for this revision"
                      : step.active
                        ? "Current step"
                        : "Pending / historical"}
                  </small>
                </li>
              ))}
            </ol>
            <ReviewForm
              key={`${proposal.id}:${proposal.version}:${proposal.revision}`}
              me={me}
              proposal={proposal}
            />
            <details>
              <summary>Immutable decisions and revisions</summary>
              <div
                className="comp-grid-scroll"
                tabIndex={0}
                role="region"
                aria-label="Proposal decision history"
              >
                <table className="comp-history">
                  <caption>
                    Recorded human decisions, including superseded revisions.
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Action</th>
                      <th scope="col">Actor</th>
                      <th scope="col">Version / review round</th>
                      <th scope="col">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {proposal.decisions.map((row) => (
                      <tr key={row.id}>
                        <td>{row.action}</td>
                        <td>{row.actor_id}</td>
                        <td>
                          {row.expected_version} ·{" "}
                          {row.round_id?.slice(0, 8) || "No Finance round"}
                        </td>
                        <td>{row.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <ul>
                {proposal.revisions.map((row) => (
                  <li key={row.id}>
                    Revision {row.number} · {row.kind} · {row.author_id} ·{" "}
                    {row.reason}
                    <code className="comp-hash">{row.sha256}</code>
                  </li>
                ))}
              </ul>
            </details>
            <details>
              <summary>
                Exact calculation inputs and monthly first-year costs
              </summary>
              <dl className="comp-pins">
                <dt>Eligibility SHA-256</dt>
                <dd>{proposal.award.inputs.eligibility_sha256}</dd>
                <dt>Guideline SHA-256</dt>
                <dd>{proposal.award.inputs.guideline_sha256}</dd>
                <dt>FX SHA-256</dt>
                <dd>{proposal.award.inputs.fx_sha256}</dd>
                <dt>Revision SHA-256</dt>
                <dd>{proposal.revision_sha256}</dd>
                <dt>Award SHA-256</dt>
                <dd>{proposal.award.sha256}</dd>
                <dt>Bonus service</dt>
                <dd>
                  {proposal.award.bonus_service_fte_days} FTE days /{" "}
                  {proposal.award.bonus_service_calendar_days} calendar days
                </dd>
              </dl>
              <ul>
                {proposal.award.merit_months.map((month) => (
                  <li key={month.month}>
                    {month.month}: {money(month.merit_cost_minor)}
                  </li>
                ))}
              </ul>
            </details>
          </>
        )
      )}
    </section>
  );
}
