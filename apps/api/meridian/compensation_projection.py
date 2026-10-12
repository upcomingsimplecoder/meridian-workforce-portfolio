"""Role-scoped response projections; auditor evidence contains no person or award rows."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from meridian.compensation_approvals import approvals_for, next_step, required_exceptions
from meridian.compensation_auth import full_cycle_access, visible_pool_ids, visible_proposals
from meridian.compensation_exports import canonical_bytes, verify_manifest
from meridian.compensation_models import (
    CompApprovalStep,
    CompCycle,
    CompDecision,
    CompEligibility,
    CompExportArtifact,
    CompLock,
    CompProposal,
    CompProposalRevision,
    CompReconciliation,
    CompRevisionVerification,
)
from meridian.compensation_repository import load_guideline
from meridian.compensation_schemas import (
    CompDecisionView,
    CompEvidenceView,
    CompRevisionView,
    CompStepView,
    CycleView,
    PoolView,
    ProposalView,
)
from meridian.compensation_service import checked_award, pool_totals, revision_for
from meridian.errors import DomainError
from meridian.identity import Actor
from meridian.object_store import read_private
from meridian.people_models import EmployeeVersion
from meridian.policy import canonical_hash


def proposal_view(
    db: Session,
    cycle: CompCycle,
    proposal: CompProposal,
    *,
    number: int | None = None,
    history: bool = True,
) -> ProposalView:
    revision = revision_for(db, proposal, number)
    membership = db.get(CompEligibility, (cycle.id, proposal.employee_id))
    if membership is None or membership.pool_id is None:
        raise DomainError(503, "comp_roster_unavailable", "The frozen roster is unavailable.")
    frozen = membership.document
    source = db.scalar(
        select(EmployeeVersion).where(EmployeeVersion.id == frozen["chain"][0]["version_id"])
    )
    if source is None:
        raise DomainError(503, "comp_source_unavailable", "The pinned source is unavailable.")
    award = checked_award(db, cycle, proposal, revision)
    approved = approvals_for(db, cycle, proposal, revision.id)
    pending = (
        next_step(db, cycle, proposal) if revision.number == proposal.current_revision else None
    )
    steps = []
    for step in db.scalars(
        select(CompApprovalStep)
        .where(CompApprovalStep.revision_id == revision.id)
        .order_by(CompApprovalStep.ordinal)
    ):
        decision = approved.get(step.ordinal)
        steps.append(
            CompStepView(
                ordinal=step.ordinal,
                role=step.role,
                assigned_id=step.assigned_id,
                capability=step.capability,
                required_exceptions=required_exceptions(db, cycle, proposal, step),
                approved=decision is not None,
                decision_id=decision.id if decision else None,
                active=pending is not None
                and pending.ordinal == step.ordinal
                and (step.ordinal != 3 or cycle.state == "FINAL_APPROVAL"),
            )
        )
    revision_query = (
        select(CompProposalRevision)
        .where(CompProposalRevision.proposal_id == proposal.id)
        .order_by(CompProposalRevision.number)
    )
    decision_query = (
        select(CompDecision)
        .where(CompDecision.proposal_id == proposal.id)
        .order_by(CompDecision.expected_version)
    )
    if not history:
        revision_query = revision_query.where(CompProposalRevision.id == revision.id)
        decision_query = decision_query.where(CompDecision.revision_id == revision.id)
    values: dict[str, Any] = {
        "id": proposal.id,
        "cycle_id": cycle.id,
        "employee_id": proposal.employee_id,
        "display_name": source.display_name,
        "proposer_id": proposal.proposer_id,
        "pool_id": membership.pool_id,
        "state": proposal.state,
        "version": proposal.version,
        "revision": revision.number,
        "current_revision": proposal.current_revision,
        "revision_id": revision.id,
        "base_pay": {
            "minor_units": frozen["base_pay_minor"],
            "currency": frozen["currency"],
            "exponent": award["currency_exponent"],
        },
        "fte": frozen["fte"],
        "comment": revision.comment,
        "award": award,
        "revision_sha256": revision.sha256,
        "verified": db.get(CompRevisionVerification, revision.id) is not None,
        "steps": steps,
        "revisions": [
            CompRevisionView.model_validate(row, from_attributes=True)
            for row in db.scalars(revision_query)
        ],
        "decisions": [
            CompDecisionView.model_validate(row, from_attributes=True)
            for row in db.scalars(decision_query)
        ],
    }
    return ProposalView.model_validate(values)


def cycle_view(db: Session, current: Actor, cycle: CompCycle) -> CycleView:
    guideline = load_guideline(db, cycle.guideline_id)
    permitted_pools = visible_pool_ids(db, current, cycle)
    count = (
        db.scalar(
            select(func.count()).select_from(visible_proposals(db, current, cycle).subquery())
        )
        or 0
    )
    excluded = (
        db.scalar(
            select(func.count())
            .select_from(CompEligibility)
            .where(CompEligibility.cycle_id == cycle.id, CompEligibility.eligible.is_(False))
        )
        if full_cycle_access(current, cycle)
        else None
    )
    values = {
        "id": cycle.id,
        "title": cycle.title,
        "owner_id": cycle.owner_id,
        "snapshot_id": cycle.snapshot_id,
        "guideline_id": cycle.guideline_id,
        "guideline": guideline,
        "guideline_sha256": guideline.sha256,
        "fx_set_id": cycle.fx_set_id,
        "authz_policy_id": cycle.authz_policy_id,
        "inputs_sha256": cycle.inputs_sha256,
        "terms": cycle.terms,
        "state": cycle.state,
        "version": cycle.version,
        "current_round_id": cycle.current_round_id,
        "visible_proposals": count,
        "configured_population_count": len(cycle.selected_ids)
        if full_cycle_access(current, cycle)
        else None,
        "ineligible_count": excluded if cycle.state not in {"CONFIGURING", "CANCELLED"} else None,
        "pools": [
            PoolView.model_validate(pool)
            for pool in pool_totals(db, cycle)
            if pool["id"] in permitted_pools
        ],
        "export_ready": db.get(CompExportArtifact, cycle.id) is not None,
        "export_failed": cycle.state == "FAILED_EXPORT",
        "created_at": cycle.created_at,
    }
    return CycleView.model_validate(values)


def evidence_view(db: Session, cycle: CompCycle) -> CompEvidenceView:
    locked = db.scalar(select(CompLock).where(CompLock.cycle_id == cycle.id))
    artifact = db.get(CompExportArtifact, cycle.id)
    verified: bool | None = None
    if artifact:
        try:
            verified = (
                artifact.sha256 == canonical_hash(artifact.document)
                and verify_manifest(artifact.document)
                and read_private(artifact.manifest_key) == canonical_bytes(artifact.document)
                and len(read_private(artifact.csv_key)) > 0
            )
        except OSError as error:
            raise DomainError(
                503,
                "comp_evidence_storage_unavailable",
                "The private artifact could not be verified.",
            ) from error
    decision_count = (
        db.scalar(
            select(func.count())
            .select_from(CompDecision)
            .join(CompProposal, CompDecision.proposal_id == CompProposal.id)
            .where(CompProposal.cycle_id == cycle.id)
        )
        or 0
    )
    return CompEvidenceView.model_validate(
        {
            "cycle_id": cycle.id,
            "title": "Compensation review " + cycle.id[:8],
            "state": cycle.state,
            "snapshot_id": cycle.snapshot_id,
            "guideline_id": cycle.guideline_id,
            "fx_set_id": cycle.fx_set_id,
            "authz_policy_id": cycle.authz_policy_id,
            "inputs_sha256": cycle.inputs_sha256,
            "locked_sha256": locked.sha256 if locked else None,
            "manifest_sha256": artifact.sha256 if artifact else None,
            "row_count": artifact.row_count if artifact else None,
            "totals_by_currency": artifact.document["manifest"]["totals_by_currency"]
            if artifact
            else None,
            "signature_verified": verified,
            "decision_count": decision_count,
            "reconciled": db.get(CompReconciliation, cycle.id) is not None,
        }
    )
