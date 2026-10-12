"""Serial human approvals, invalidated Finance rounds and atomic final locking."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.compensation_auth import validate_assignments
from meridian.compensation_models import (
    CompApprovalStep,
    CompCycle,
    CompDecision,
    CompEligibility,
    CompLock,
    CompProposal,
    CompReviewRound,
    CompRevisionVerification,
)
from meridian.compensation_service import (
    check_inputs,
    checked_award,
    latest_rows,
    pool_totals,
    record_cycle_action,
    revision_event,
    revision_for,
    save_revision,
)
from meridian.errors import DomainError
from meridian.finance_repository import exact_decimal, load_fx
from meridian.governance import record
from meridian.models import uid
from meridian.people_models import Snapshot
from meridian.policy import POLICY_ID, canonical_hash


def approvals_for(
    db: Session, cycle: CompCycle, proposal: CompProposal, revision_id: str
) -> dict[int, CompDecision]:
    return {
        decision.ordinal: decision
        for decision in db.scalars(
            select(CompDecision)
            .where(
                CompDecision.proposal_id == proposal.id,
                CompDecision.revision_id == revision_id,
                CompDecision.action == "APPROVE",
            )
            .order_by(CompDecision.expected_version)
        )
        if decision.ordinal != 3 or decision.round_id == cycle.current_round_id
    }


def next_step(db: Session, cycle: CompCycle, proposal: CompProposal) -> CompApprovalStep | None:
    if proposal.state not in {"SUBMITTED", "APPROVAL_PENDING"}:
        return None
    revision = revision_for(db, proposal)
    approved = approvals_for(db, cycle, proposal, revision.id)
    return next(
        (
            step
            for step in db.scalars(
                select(CompApprovalStep)
                .where(CompApprovalStep.revision_id == revision.id)
                .order_by(CompApprovalStep.ordinal)
            )
            if step.ordinal not in approved
        ),
        None,
    )


def required_exceptions(
    db: Session, cycle: CompCycle, proposal: CompProposal, step: CompApprovalStep
) -> list[str]:
    if step.ordinal != 3:
        return step.exception_codes
    membership = db.get(CompEligibility, (cycle.id, proposal.employee_id))
    review_round = (
        db.get(CompReviewRound, cycle.current_round_id) if cycle.current_round_id else None
    )
    if membership is None or review_round is None:
        return []
    return next(
        pool["exceptions"]
        for pool in review_round.document["pools"]
        if pool["id"] == membership.pool_id
    )


def make_decision(
    db: Session,
    cycle: CompCycle,
    proposal: CompProposal,
    *,
    actor_id: str,
    action: str,
    ordinal: int,
    reason: str,
    exceptions: list[str],
    request_id: str,
    round_id: str | None = None,
) -> CompDecision:
    revision = revision_for(db, proposal)
    conflict = {
        "actor_id": actor_id,
        "proposer_id": proposal.proposer_id,
        "revision_sha256": revision.sha256,
        "cycle_inputs_sha256": cycle.inputs_sha256,
        "ordinal": ordinal,
        "round_id": round_id,
        "current_policy": POLICY_ID,
        "exceptions": exceptions,
        "reason": reason,
    }
    decision = CompDecision(
        proposal_id=proposal.id,
        revision_id=revision.id,
        ordinal=ordinal,
        round_id=round_id,
        actor_id=actor_id,
        expected_version=proposal.version,
        action=action,
        reason=reason,
        acknowledged_exceptions=exceptions,
        conflict_check_sha256=canonical_hash(conflict),
        request_id=request_id,
    )
    db.add(decision)
    db.flush()
    record(
        db,
        actor_id,
        f"comp.proposal_{action.lower()}",
        "comp_proposal",
        proposal.id,
        request_id,
        before_hash=revision.sha256,
        after_hash=decision.conflict_check_sha256,
        entity_version=proposal.version + 1,
        emit=False,
    )
    return decision


def submit_proposal(
    db: Session, cycle: CompCycle, proposal: CompProposal, *, reason: str, request_id: str
) -> None:
    if proposal.state != "DRAFT" or cycle.state not in {"OPEN", "REVIEWING"}:
        raise DomainError(
            409, "comp_proposal_closed", "Only an open manager draft can be submitted."
        )
    revision = revision_for(db, proposal)
    if not db.get(CompRevisionVerification, revision.id):
        raise DomainError(
            409,
            "comp_verification_pending",
            "Wait for independent worker verification before submitting.",
        )
    if len(revision.comment.strip()) < 3 or revision.kind != "EDIT":
        raise DomainError(
            422,
            "comp_justification_required",
            "Save an explicit manager award and rationale before submitting.",
        )
    validate_assignments(db, cycle)
    if any(pool["blockers"] for pool in pool_totals(db, cycle)):
        raise DomainError(422, "comp_pool_budget_exceeded", "A configured pool exceeds its budget.")
    make_decision(
        db,
        cycle,
        proposal,
        actor_id=proposal.proposer_id,
        action="SUBMIT",
        ordinal=0,
        reason=reason,
        exceptions=[],
        request_id=request_id,
    )
    proposal.state = "SUBMITTED"
    proposal.version += 1
    db.flush()
    if cycle.state == "OPEN":
        record_cycle_action(
            db,
            cycle,
            action="REVIEWING",
            actor_id=proposal.proposer_id,
            reason="First verified manager submission",
            evidence=revision.sha256,
            request_id=request_id,
            state="REVIEWING",
        )
    revision_event(
        db, cycle, proposal, revision, proposal.proposer_id, request_id, "comp.proposal_submitted"
    )


def start_review(
    db: Session,
    cycle: CompCycle,
    proposal: CompProposal,
    *,
    actor_id: str,
    reason: str,
    request_id: str,
) -> None:
    if proposal.state != "SUBMITTED" or cycle.state != "REVIEWING":
        raise DomainError(
            409, "comp_review_unavailable", "This proposal is not awaiting its division review."
        )
    make_decision(
        db,
        cycle,
        proposal,
        actor_id=actor_id,
        action="START_REVIEW",
        ordinal=1,
        reason=reason,
        exceptions=[],
        request_id=request_id,
    )
    proposal.state = "APPROVAL_PENDING"
    proposal.version += 1
    db.flush()


def finance_document(db: Session, cycle: CompCycle) -> dict[str, Any]:
    totals: dict[str, dict[str, int]] = {}
    references: list[dict[str, Any]] = []
    reporting = 0
    for proposal, revision in latest_rows(db, cycle.id):
        award = checked_award(db, cycle, proposal, revision)
        values = totals.setdefault(
            award["currency"],
            {
                field: 0
                for field in (
                    "annualized_merit_minor",
                    "merit_first_year_minor",
                    "bonus_payable_minor",
                    "pool_charge_minor",
                )
            },
        )
        for field in values:
            values[field] += int(award[field])
        reporting += int(award["reporting_pool_charge_minor"])
        references.append(
            {
                "proposal_id": proposal.id,
                "revision_id": revision.id,
                "revision_sha256": revision.sha256,
                "award_sha256": award["sha256"],
            }
        )
    return {
        "cycle_id": cycle.id,
        "inputs_sha256": cycle.inputs_sha256,
        "proposal_revisions": references,
        "pools": pool_totals(db, cycle),
        "row_count": len(references),
        "totals_by_currency": {
            currency: {field: str(amount) for field, amount in values.items()}
            for currency, values in sorted(totals.items())
        },
        "reporting_currency": load_fx(db, cycle.fx_set_id).reporting_currency,
        "reporting_pool_charge_minor": str(reporting),
    }


def maybe_open_finance(db: Session, cycle: CompCycle, *, actor_id: str, request_id: str) -> None:
    if cycle.state != "REVIEWING":
        return
    rows = latest_rows(db, cycle.id)
    if not rows or any(
        proposal.state != "APPROVAL_PENDING"
        or 2 not in approvals_for(db, cycle, proposal, revision.id)
        for proposal, revision in rows
    ):
        return
    check_inputs(db, cycle)
    validate_assignments(db, cycle)
    document = finance_document(db, cycle)
    if any(pool["blockers"] for pool in document["pools"]):
        raise DomainError(
            422, "comp_pool_budget_exceeded", "The final Finance round exceeds a configured budget."
        )
    number = (
        len(
            list(db.scalars(select(CompReviewRound.id).where(CompReviewRound.cycle_id == cycle.id)))
        )
        + 1
    )
    review_round = CompReviewRound(
        id=uid(),
        cycle_id=cycle.id,
        number=number,
        document=document,
        sha256=canonical_hash(document),
    )
    db.add(review_round)
    db.flush()
    record_cycle_action(
        db,
        cycle,
        action="OPEN_FINANCE",
        actor_id=actor_id,
        reason="All current division and HR signatures completed",
        evidence=review_round.sha256,
        request_id=request_id,
        state="FINAL_APPROVAL",
        round_id=review_round.id,
    )


def decide(
    db: Session,
    cycle: CompCycle,
    proposal: CompProposal,
    *,
    actor_id: str,
    action: str,
    reason: str,
    exceptions: list[str],
    request_id: str,
) -> None:
    step = next_step(db, cycle, proposal)
    if (
        proposal.state != "APPROVAL_PENDING"
        or step is None
        or step.ordinal == 3
        and cycle.state != "FINAL_APPROVAL"
    ):
        raise DomainError(
            409, "comp_review_unavailable", "The proposal is not awaiting this serial review step."
        )
    if step.assigned_id != actor_id or proposal.proposer_id == actor_id:
        raise DomainError(
            403, "comp_assigned_reviewer_required", "Only the named separate reviewer can decide."
        )
    validate_assignments(db, cycle)
    needed = required_exceptions(db, cycle, proposal, step)
    if action == "APPROVE" and (
        len(set(exceptions)) != len(exceptions) or set(exceptions) != set(needed)
    ):
        raise DomainError(
            422,
            "comp_exception_acknowledgement_required",
            "Explicitly acknowledge the exact exceptions assigned to this review step.",
        )
    ordered_exceptions = needed if action == "APPROVE" else []
    old_revision = revision_for(db, proposal)
    make_decision(
        db,
        cycle,
        proposal,
        actor_id=actor_id,
        action=action,
        ordinal=step.ordinal,
        reason=reason,
        exceptions=ordered_exceptions,
        request_id=request_id,
        round_id=cycle.current_round_id if step.ordinal == 3 else None,
    )
    if action == "RETURN":
        old_round = cycle.current_round_id
        save_revision(
            db,
            cycle,
            proposal,
            merit_minor=exact_decimal(old_revision.merit_minor),
            bonus_minor=exact_decimal(old_revision.bonus_minor),
            comment=old_revision.comment,
            reason=reason,
            author_id=actor_id,
            kind="RETURN",
            request_id=request_id,
        )
        revision_event(
            db,
            cycle,
            proposal,
            revision_for(db, proposal),
            actor_id,
            request_id,
            "comp.proposal_returned",
        )
        if old_round is not None:
            record_cycle_action(
                db,
                cycle,
                action="RETURN_TO_REVIEW",
                actor_id=actor_id,
                reason=reason,
                evidence=old_revision.sha256,
                request_id=request_id,
                state="REVIEWING",
            )
            # Previous Finance signatures remain immutable evidence, but cannot lock a
            # changed financial bundle. All already-approved siblings explicitly reopen.
            for sibling, _revision in latest_rows(db, cycle.id):
                if sibling.state != "APPROVED":
                    continue
                make_decision(
                    db,
                    cycle,
                    sibling,
                    actor_id=actor_id,
                    action="INVALIDATE_FINANCE",
                    ordinal=3,
                    reason="Finance round invalidated by a returned proposal",
                    exceptions=[],
                    request_id=request_id,
                    round_id=old_round,
                )
                sibling.state = "APPROVAL_PENDING"
                sibling.version += 1
                db.flush()
        return
    proposal.state = "APPROVED" if step.ordinal == 3 else "APPROVAL_PENDING"
    proposal.version += 1
    db.flush()
    maybe_open_finance(db, cycle, actor_id=actor_id, request_id=request_id)


def lock_cycle(db: Session, cycle: CompCycle, *, reason: str, request_id: str) -> CompLock:
    if cycle.state != "FINAL_APPROVAL" or cycle.current_round_id is None:
        raise DomainError(
            409,
            "comp_lock_unavailable",
            "A complete current Finance round is required before locking.",
        )
    check_inputs(db, cycle)
    validate_assignments(db, cycle)
    review_round = db.get(CompReviewRound, cycle.current_round_id)
    document = finance_document(db, cycle)
    if (
        review_round is None
        or review_round.sha256 != canonical_hash(review_round.document)
        or document != review_round.document
    ):
        raise DomainError(
            409,
            "comp_finance_round_stale",
            "The financial bundle changed and requires a new Finance round.",
        )
    rows = latest_rows(db, cycle.id)
    if any(
        proposal.state != "APPROVED"
        or not {1, 2, 3} <= set(approvals_for(db, cycle, proposal, revision.id))
        or db.get(CompRevisionVerification, revision.id) is None
        for proposal, revision in rows
    ):
        raise DomainError(
            409,
            "comp_signatures_incomplete",
            "Every current proposal requires all independent signatures and verification.",
        )
    if any(proposal.proposer_id == cycle.owner_id for proposal, _ in rows):
        raise DomainError(
            403, "comp_self_finalize_denied", "The controller cannot finalize their own proposals."
        )
    decisions = list(
        db.scalars(
            select(CompDecision)
            .join(CompProposal, CompDecision.proposal_id == CompProposal.id)
            .where(CompProposal.cycle_id == cycle.id)
            .order_by(CompProposal.employee_id, CompDecision.expected_version)
        )
    )
    locked_document = document | {
        "review_round_id": review_round.id,
        "review_round_sha256": review_round.sha256,
        "inputs": check_inputs(db, cycle),
        "decisions": [
            {
                "id": decision.id,
                "proposal_id": decision.proposal_id,
                "revision_id": decision.revision_id,
                "actor_id": decision.actor_id,
                "action": decision.action,
                "ordinal": decision.ordinal,
                "round_id": decision.round_id,
                "reason": decision.reason,
                "acknowledged_exceptions": decision.acknowledged_exceptions,
                "conflict_check_sha256": decision.conflict_check_sha256,
                "request_id": decision.request_id,
                "created_at": decision.created_at.isoformat(),
            }
            for decision in decisions
        ],
    }
    sha = canonical_hash(locked_document)
    action = record_cycle_action(
        db,
        cycle,
        action="LOCK",
        actor_id=cycle.owner_id,
        reason=reason,
        evidence=sha,
        request_id=request_id,
        state="LOCKED",
    )
    locked = CompLock(
        id=uid(),
        cycle_id=cycle.id,
        round_id=review_round.id,
        action_id=action.id,
        document=locked_document,
        sha256=sha,
    )
    db.add(locked)
    db.flush()
    cycle.state = "LOCKED"
    cycle.version += 1
    db.flush()
    for proposal, _ in rows:
        proposal.state = "LOCKED"
        proposal.version += 1
        db.flush()
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    if snapshot is None:
        raise DomainError(503, "comp_source_unavailable", "The pinned source is unavailable.")
    record(
        db,
        cycle.owner_id,
        "comp.cycle_locked",
        "comp_cycle",
        cycle.id,
        request_id,
        after_hash=sha,
        entity_version=cycle.version,
        event_payload={
            "entity_type": "comp_cycle",
            "entity_id": cycle.id,
            "lock_id": locked.id,
            "source_batch_id": snapshot.source_batch_id,
        },
    )
    return locked
