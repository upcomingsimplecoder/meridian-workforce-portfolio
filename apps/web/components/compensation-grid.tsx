"use client";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  compMoney,
  moneyInput,
  parseMoneyInput,
  proposalCommand,
  type Cycle,
  type Proposal,
} from "@/lib/compensation";

function CompensationRow({
  me,
  cycle,
  proposal,
  onInspect,
}: {
  me: Me;
  cycle: Cycle;
  proposal: Proposal;
  onInspect: (id: string) => void;
}) {
  const exponent = proposal.award.currency_exponent;
  const [merit, setMerit] = useState(
    moneyInput(proposal.award.merit_minor, exponent),
  );
  const [bonus, setBonus] = useState(
    moneyInput(proposal.award.bonus_target_minor, exponent),
  );
  const [comment, setComment] = useState(proposal.comment);
  const [error, setError] = useState("");
  const client = useQueryClient();
  const owner =
    me.id === proposal.proposer_id && me.capabilities.includes("comp.propose");
  const editable =
    owner &&
    ["DRAFT", "RETURNED"].includes(proposal.state) &&
    ["OPEN", "REVIEWING"].includes(cycle.state);
  const dirty =
    merit !== moneyInput(proposal.award.merit_minor, exponent) ||
    bonus !== moneyInput(proposal.award.bonus_target_minor, exponent) ||
    comment !== proposal.comment;
  const mutation = useMutation({
    mutationFn: (action: "save" | "submit") =>
      proposalCommand(
        me,
        proposal,
        action === "save"
          ? {
              action: "award",
              body: {
                merit_minor: parseMoneyInput(merit, exponent),
                bonus_minor: parseMoneyInput(bonus, exponent),
                comment,
                reason: "Manager saved proposed award and rationale",
              },
            }
          : {
              action: "submit",
              body: { reason: "Manager submitted verified saved proposal" },
            },
        crypto.randomUUID(),
      ),
    onMutate: () => setError(""),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["comp", me.id] });
    },
    onError: (err) => setError(err.message),
  });
  function save() {
    if (!editable || !dirty || mutation.isPending || error) return;
    // Keep incomplete decimals/rationale locally; they are never shown as acknowledged.
    try {
      parseMoneyInput(merit, exponent);
      parseMoneyInput(bonus, exponent);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Check the amounts.");
      return;
    }
    mutation.mutate("save");
  }
  function change(setter: (value: string) => void, value: string) {
    setter(value);
    setError("");
  }
  const money = (minor: string) =>
    compMoney(minor, proposal.award.currency, exponent);
  return (
    <tr
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) save();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
          event.preventDefault();
          save();
        }
      }}
    >
      <th scope="row">
        <button
          className="comp-person-link"
          onClick={() => onInspect(proposal.id)}
        >
          {proposal.display_name}
        </button>
        <small>
          {proposal.employee_id} · {proposal.award.org_id}
        </small>
      </th>
      <td className="numeric">
        {money(proposal.base_pay.minor_units)}
        <small>{proposal.fte} FTE · full-time base</small>
      </td>
      <td>
        {editable ? (
          <input
            aria-label={`Merit amount ${proposal.employee_id}`}
            inputMode="decimal"
            disabled={mutation.isPending}
            value={merit}
            onChange={(event) => change(setMerit, event.target.value)}
          />
        ) : (
          money(proposal.award.merit_minor)
        )}
        <small>Annual full-time increment</small>
      </td>
      <td>
        {editable ? (
          <input
            aria-label={`Bonus target ${proposal.employee_id}`}
            inputMode="decimal"
            disabled={mutation.isPending}
            value={bonus}
            onChange={(event) => change(setBonus, event.target.value)}
          />
        ) : (
          money(proposal.award.bonus_target_minor)
        )}
        <small>Full-time / full-service target</small>
      </td>
      <td className="numeric">
        {money(proposal.award.annualized_merit_minor)}
        <small>First year {money(proposal.award.merit_first_year_minor)}</small>
      </td>
      <td className="numeric">{money(proposal.award.bonus_payable_minor)}</td>
      <td className="numeric">
        <strong>{money(proposal.award.pool_charge_minor)}</strong>
      </td>
      <td>
        {editable ? (
          <textarea
            aria-label={`Award rationale ${proposal.employee_id}`}
            rows={2}
            maxLength={500}
            disabled={mutation.isPending}
            value={comment}
            onChange={(event) => change(setComment, event.target.value)}
          />
        ) : (
          <span className="comp-comment">
            {proposal.comment || "No manager rationale yet"}
          </span>
        )}
      </td>
      <td>
        <span className="status-tag">
          {proposal.state.replaceAll("_", " ")}
        </span>
        <small>
          Revision {proposal.revision} · version {proposal.version}
        </small>
        {proposal.award.exceptions.map((code) => (
          <small key={code} className="comp-exception">
            {code.replaceAll("_", " ")}
          </small>
        ))}
        {proposal.award.warnings.map((code) => (
          <small key={code}>{code.replaceAll("_", " ")}</small>
        ))}
      </td>
      <td>
        <div className="comp-row-actions">
          {editable && (
            <button
              className="button"
              disabled={!dirty || mutation.isPending}
              onClick={() => {
                setError("");
                mutation.mutate("save");
              }}
            >
              Save award
            </button>
          )}
          {editable && (
            <button
              className="button primary"
              disabled={
                dirty ||
                mutation.isPending ||
                !proposal.verified ||
                proposal.revisions.at(-1)?.kind !== "EDIT" ||
                proposal.comment.trim().length < 3
              }
              onClick={() => mutation.mutate("submit")}
            >
              Submit proposal
            </button>
          )}
          <button className="button" onClick={() => onInspect(proposal.id)}>
            Review details
          </button>
          <small role="status">
            {mutation.isPending
              ? "Saving to the server…"
              : dirty
                ? "Unsaved · saves when leaving this row"
                : proposal.verified
                  ? "Saved · independently verified"
                  : "Saved · awaiting worker verification"}
          </small>
          {error && (
            <div role="alert" className="comp-row-error">
              {error}
              <button
                className="button"
                onClick={() =>
                  client.invalidateQueries({ queryKey: ["comp", me.id] })
                }
              >
                Refresh and discard local draft
              </button>
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}

export function CompensationGrid({
  me,
  cycle,
  proposals,
  onInspect,
}: {
  me: Me;
  cycle: Cycle;
  proposals: Proposal[];
  onInspect: (id: string) => void;
}) {
  return (
    <div
      className="comp-grid-scroll"
      role="region"
      tabIndex={0}
      aria-label="Authorized compensation proposal table"
    >
      <table className="comp-grid">
        <caption>
          Saved awards in original currency. Changes reserve annualized merit
          plus payable bonus.
        </caption>
        <thead>
          <tr>
            {[
              "Employee",
              "Current base / FTE",
              "Merit amount",
              "Bonus target",
              "Merit cost",
              "Payable bonus",
              "Pool charge",
              "Rationale",
              "Governance",
              "Actions / save state",
            ].map((label) => (
              <th scope="col" key={label}>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {proposals.map((proposal) => (
            <CompensationRow
              key={`${proposal.id}:${proposal.version}`}
              me={me}
              cycle={cycle}
              proposal={proposal}
              onInspect={onInspect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
