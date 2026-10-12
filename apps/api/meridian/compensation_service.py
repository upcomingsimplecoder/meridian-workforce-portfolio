"""Persisted compensation inputs, revisions, assigned budgets and independent verification."""

import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.compensation_engine import (
    ChainLink,
    CycleTerms,
    FrozenEligibility,
    Pool,
    calculate_award,
    evaluate_pool,
)
from meridian.compensation_models import (
    CompApprovalStep,
    CompCycle,
    CompCycleAction,
    CompEligibility,
    CompPool,
    CompProposal,
    CompProposalRevision,
    CompRevisionVerification,
)
from meridian.compensation_repository import load_eligibility, load_guideline
from meridian.compensation_schemas import CycleCreate
from meridian.errors import DomainError
from meridian.finance_engine import CostAssignment, FinanceInputError, validate_history
from meridian.finance_repository import exact_decimal, load_fx
from meridian.governance import record
from meridian.models import uid
from meridian.people_models import AuthzPolicy, EmployeeVersion, OrgUnit, Snapshot
from meridian.policy import canonical_hash

SERIAL_STEPS = (
    (1, "DIVISION", "division_approver_id", "comp.approve"),
    (2, "HR", "hr_signoff_id", "comp.hr.signoff"),
    (3, "FINANCE", "finance_signoff_id", "comp.finance.signoff"),
)


def prepare_cycle(
    db: Session, body: CycleCreate, owner_id: str, policy_id: str
) -> tuple[CompCycle, list[CompPool], tuple[FrozenEligibility, ...]]:
    population = load_eligibility(
        db,
        snapshot_id=str(body.snapshot_id),
        guideline_id=str(body.guideline_id),
        terms=CycleTerms.model_validate(body.terms.model_dump()),
        selected_ids=tuple(body.selected_ids),
    )
    needed_pools = {
        (row.proposer_id, row.org_id, row.currency) for row in population if row.eligible
    }
    pool_keys = [(pool.manager_id, pool.org_id, pool.currency) for pool in body.pools]
    # Raw cycle controllers can inspect the full pinned recipient history, including
    # prior departments. Keep those departments in the field/object authorization pin.
    scope = sorted({period.org_id for row in population for period in row.history})
    if not needed_pools or len(set(pool_keys)) != len(pool_keys) or set(pool_keys) != needed_pools:
        raise DomainError(
            422,
            "comp_pool_assignment_mismatch",
            "Configure exactly one pool for each eligible manager, department and currency.",
        )
    if len({route.org_id for route in body.approvals}) != len(body.approvals) or {
        route.org_id for route in body.approvals
    } != {row.org_id for row in population if row.eligible}:
        raise DomainError(
            422,
            "comp_approval_assignment_mismatch",
            "Every eligible department needs exactly one serial approval assignment.",
        )
    for assignment in body.approvals:
        reviewers = (
            assignment.division_approver_id,
            assignment.hr_signoff_id,
            assignment.finance_signoff_id,
        )
        proposers = {
            row.proposer_id
            for row in population
            if row.org_id == assignment.org_id and row.eligible
        }
        if len(set(reviewers)) != 3 or proposers.intersection(reviewers):
            raise DomainError(
                403,
                "comp_separation_of_duties",
                "Three separate assigned reviewers must be distinct from every proposer in the department.",
            )
    cycle = CompCycle(
        id=uid(),
        title=body.title,
        owner_id=owner_id,
        snapshot_id=str(body.snapshot_id),
        guideline_id=str(body.guideline_id),
        fx_set_id=str(body.fx_set_id),
        authz_policy_id=policy_id,
        terms=body.terms.document(),
        selected_ids=sorted(body.selected_ids),
        scope_orgs=scope,
        approvals=[
            route.model_dump() for route in sorted(body.approvals, key=lambda route: route.org_id)
        ],
        state="CONFIGURING",
        version=1,
        current_round_id=None,
    )
    pools: list[CompPool] = []
    for requested in body.pools:
        org = db.get(OrgUnit, requested.org_id)
        if org is None:
            raise FinanceInputError("COMPENSATION_ORGANIZATION_UNAVAILABLE")
        Pool(
            requested.org_id,
            requested.currency,
            org.cost_center,
            requested.budget_minor,
            requested.holdback_minor,
        ).validate()
        pools.append(
            CompPool(
                id=uid(),
                cycle_id=cycle.id,
                manager_id=requested.manager_id,
                org_id=requested.org_id,
                currency=requested.currency,
                cost_center=org.cost_center,
                budget_minor=Decimal(requested.budget_minor),
                holdback_minor=Decimal(requested.holdback_minor),
            )
        )
    cycle.inputs_sha256 = canonical_hash(cycle_inputs(db, cycle, pools))
    return cycle, pools, population


def terms_for(cycle: CompCycle) -> CycleTerms:
    return CycleTerms.model_validate_json(json.dumps(cycle.terms))


def pool_document(pool: CompPool) -> dict[str, str]:
    return Pool(
        pool.org_id,
        pool.currency,
        pool.cost_center,
        exact_decimal(pool.budget_minor),
        exact_decimal(pool.holdback_minor),
    ).document() | {"id": pool.id, "manager_id": pool.manager_id}


def pool_key(pool: CompPool) -> tuple[str, str, str]:
    return pool.manager_id, pool.org_id, pool.currency


def cycle_inputs(
    db: Session, cycle: CompCycle, configured_pools: list[CompPool] | None = None
) -> dict[str, Any]:
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    policy = db.get(AuthzPolicy, cycle.authz_policy_id)
    if (
        snapshot is None
        or snapshot.status != "PUBLISHED"
        or policy is None
        or policy.sha256 != canonical_hash(policy.definition)
    ):
        raise FinanceInputError("COMPENSATION_PINNED_SOURCE_OR_POLICY_UNAVAILABLE")
    guideline = load_guideline(db, cycle.guideline_id)
    fx = load_fx(db, cycle.fx_set_id)
    terms = terms_for(cycle)
    if guideline.effective_date > terms.eligibility_date:
        raise FinanceInputError("COMPENSATION_GUIDELINE_NOT_EFFECTIVE")
    fx.validate(
        min(terms.bonus_service_start, terms.merit_effective_date),
        max(
            date(terms.merit_effective_date.year + 1, 1, 1),
            terms.bonus_payment_date + timedelta(days=1),
        ),
    )
    pools = sorted(
        configured_pools
        if configured_pools is not None
        else db.scalars(select(CompPool).where(CompPool.cycle_id == cycle.id)),
        key=pool_key,
    )
    return {
        "snapshot_id": snapshot.id,
        "snapshot_sha256": snapshot.sha256,
        "source_batch_id": snapshot.source_batch_id,
        "guideline_id": cycle.guideline_id,
        "guideline_sha256": guideline.sha256,
        "fx_set_id": fx.id,
        "fx_sha256": fx.sha256,
        "authz_policy_id": policy.id,
        "authz_policy_sha256": policy.sha256,
        "owner_id": cycle.owner_id,
        "terms": terms.document(),
        "selected_ids": sorted(cycle.selected_ids),
        "scope_orgs": sorted(cycle.scope_orgs),
        "approvals": sorted(cycle.approvals, key=lambda assignment: assignment["org_id"]),
        "pools": [{k: v for k, v in pool_document(pool).items() if k != "id"} for pool in pools],
    }


def check_inputs(db: Session, cycle: CompCycle) -> dict[str, Any]:
    document = cycle_inputs(db, cycle)
    if canonical_hash(document) != cycle.inputs_sha256:
        raise FinanceInputError("COMPENSATION_CYCLE_INPUT_FINGERPRINT_MISMATCH")
    return document


def frozen_for(db: Session, cycle: CompCycle, employee_id: str) -> FrozenEligibility:
    stored = db.get(CompEligibility, (cycle.id, employee_id))
    if stored is None or stored.sha256 != canonical_hash(stored.document):
        raise FinanceInputError("COMPENSATION_ELIGIBILITY_UNAVAILABLE_OR_CORRUPT")
    document = stored.document
    history = tuple(
        CostAssignment(
            **(
                row
                | {
                    "valid_from": date.fromisoformat(row["valid_from"]),
                    "valid_to": date.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
                }
            )
        )
        for row in document["history"]
    )
    validate_history(list(history))
    frozen = FrozenEligibility(
        document["employee_id"],
        document["eligible"],
        tuple(document["reason_codes"]),
        document["org_id"],
        document["currency"],
        document["base_pay_minor"],
        document["fte"],
        document["proposer_id"],
        document["service_days"],
        history,
        tuple(ChainLink(**link) for link in document["chain"]),
    )
    if (
        frozen.document() != document
        or frozen.sha256 != stored.sha256
        or frozen.employee_id != employee_id
        or frozen.eligible != stored.eligible
        or frozen.proposer_id != stored.proposer_id
    ):
        raise FinanceInputError("COMPENSATION_ELIGIBILITY_FINGERPRINT_MISMATCH")
    source = list(
        db.scalars(
            select(EmployeeVersion)
            .where(
                EmployeeVersion.snapshot_id == cycle.snapshot_id,
                EmployeeVersion.employee_id == employee_id,
            )
            .order_by(EmployeeVersion.valid_from)
        )
    )
    source_fingerprints = [
        CostAssignment(
            row.employee_id,
            row.id,
            row.source_batch_id,
            row.org_id,
            row.region,
            row.valid_from,
            row.valid_to,
            row.status,
            exact_decimal(row.annual_pay_minor),
            row.currency,
            exact_decimal(row.fte),
        ).fingerprint()
        for row in source
    ]
    if source_fingerprints != [row.fingerprint() for row in history]:
        raise FinanceInputError("COMPENSATION_FROZEN_HISTORY_DIFFERS_FROM_SOURCE")
    links = {
        row.id: row
        for row in db.scalars(
            select(EmployeeVersion).where(
                EmployeeVersion.id.in_([link.version_id for link in frozen.chain])
            )
        )
    }
    for link in frozen.chain:
        row = links.get(link.version_id)
        if (
            row is None
            or row.snapshot_id != cycle.snapshot_id
            or row.employee_id != link.employee_id
            or row.manager_id != link.manager_id
            or row.source_batch_id != link.source_batch_id
            or not row.valid_from <= terms_for(cycle).eligibility_date < (row.valid_to or date.max)
        ):
            raise FinanceInputError("COMPENSATION_FROZEN_CHAIN_DIFFERS_FROM_SOURCE")
    return frozen


def revision_for(
    db: Session, proposal: CompProposal, number: int | None = None
) -> CompProposalRevision:
    revision = db.scalar(
        select(CompProposalRevision).where(
            CompProposalRevision.proposal_id == proposal.id,
            CompProposalRevision.number == (number or proposal.current_revision),
        )
    )
    if revision is None:
        raise DomainError(
            404, "comp_revision_unavailable", "The requested proposal revision is unavailable."
        )
    return revision


def revision_document(
    cycle: CompCycle, proposal: CompProposal, revision: CompProposalRevision, eligibility_sha: str
) -> dict[str, Any]:
    return {
        "cycle_id": cycle.id,
        "cycle_inputs_sha256": cycle.inputs_sha256,
        "proposal_id": proposal.id,
        "employee_id": proposal.employee_id,
        "proposer_id": proposal.proposer_id,
        "eligibility_sha256": eligibility_sha,
        "number": revision.number,
        "kind": revision.kind,
        "merit_minor": exact_decimal(revision.merit_minor),
        "bonus_minor": exact_decimal(revision.bonus_minor),
        "comment": revision.comment,
        "reason": revision.reason,
        "author_id": revision.author_id,
        "award_sha256": revision.award["sha256"],
    }


def checked_award(
    db: Session, cycle: CompCycle, proposal: CompProposal, revision: CompProposalRevision
) -> dict[str, Any]:
    stored = db.get(CompEligibility, (cycle.id, proposal.employee_id))
    if (
        stored is None
        or stored.sha256 != canonical_hash(stored.document)
        or revision.sha256
        != canonical_hash(revision_document(cycle, proposal, revision, stored.sha256))
        or revision.award["sha256"]
        != canonical_hash({k: v for k, v in revision.award.items() if k != "sha256"})
    ):
        raise FinanceInputError("COMPENSATION_REVISION_FINGERPRINT_MISMATCH")
    return revision.award


def latest_rows(db: Session, cycle_id: str) -> list[tuple[CompProposal, CompProposalRevision]]:
    return [
        (p, r)
        for p, r in db.execute(
            select(CompProposal, CompProposalRevision)
            .join(
                CompProposalRevision,
                (CompProposalRevision.proposal_id == CompProposal.id)
                & (CompProposalRevision.number == CompProposal.current_revision),
            )
            .where(CompProposal.cycle_id == cycle_id)
            .order_by(CompProposal.employee_id)
        )
    ]


def pool_totals(db: Session, cycle: CompCycle) -> list[dict[str, Any]]:
    pools = list(
        db.scalars(
            select(CompPool)
            .where(CompPool.cycle_id == cycle.id)
            .order_by(CompPool.org_id, CompPool.manager_id, CompPool.currency)
        )
    )
    memberships = {
        row.employee_id: row.pool_id
        for row in db.scalars(select(CompEligibility).where(CompEligibility.cycle_id == cycle.id))
    }
    awards: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for proposal, revision in latest_rows(db, cycle.id):
        pool_id = memberships[proposal.employee_id]
        if pool_id is None:
            raise FinanceInputError("COMPENSATION_ELIGIBLE_POOL_UNAVAILABLE")
        awards[pool_id].append(checked_award(db, cycle, proposal, revision))
    return [
        evaluate_pool(
            Pool(
                pool.org_id,
                pool.currency,
                pool.cost_center,
                exact_decimal(pool.budget_minor),
                exact_decimal(pool.holdback_minor),
            ),
            awards[pool.id],
        )
        | {"id": pool.id, "manager_id": pool.manager_id}
        for pool in pools
    ]


def record_cycle_action(
    db: Session,
    cycle: CompCycle,
    *,
    action: str,
    actor_id: str | None,
    reason: str,
    evidence: str,
    request_id: str,
    state: str,
    round_id: str | None = None,
) -> CompCycleAction:
    stored = CompCycleAction(
        cycle_id=cycle.id,
        expected_version=cycle.version,
        actor_id=actor_id,
        action=action,
        reason=reason,
        evidence_sha256=evidence,
        request_id=request_id,
    )
    db.add(stored)
    db.flush()
    # LOCK evidence is inserted while the parent remains FINAL_APPROVAL.
    # The finalizer advances the parent after that evidence is flushed.
    if action != "LOCK":
        cycle.state = state
        cycle.version += 1
        if action in {"OPEN_FINANCE", "RETURN_TO_REVIEW"}:
            cycle.current_round_id = round_id
        db.flush()
    record(
        db,
        actor_id,
        f"comp.cycle_{action.lower()}",
        "comp_cycle",
        cycle.id,
        request_id,
        before_hash=None,
        after_hash=evidence,
        entity_version=cycle.version + (action == "LOCK"),
        emit=False,
    )
    return stored


def revision_event(
    db: Session,
    cycle: CompCycle,
    proposal: CompProposal,
    revision: CompProposalRevision,
    actor_id: str,
    request_id: str,
    verb: str = "comp.proposal_revision_created",
) -> None:
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    if snapshot is None:
        raise FinanceInputError("COMPENSATION_PINNED_SNAPSHOT_UNAVAILABLE")
    record(
        db,
        actor_id,
        verb,
        "comp_proposal",
        proposal.id,
        request_id,
        after_hash=revision.sha256,
        entity_version=proposal.version,
        event_payload={
            "entity_type": "comp_proposal",
            "entity_id": proposal.id,
            "cycle_id": cycle.id,
            "revision_id": revision.id,
            "source_batch_id": snapshot.source_batch_id,
        },
    )


def save_revision(
    db: Session,
    cycle: CompCycle,
    proposal: CompProposal,
    *,
    merit_minor: str,
    bonus_minor: str,
    comment: str,
    reason: str,
    author_id: str,
    kind: str,
    request_id: str,
) -> CompProposalRevision:
    check_inputs(db, cycle)
    frozen = frozen_for(db, cycle, proposal.employee_id)
    award = calculate_award(
        frozen,
        load_guideline(db, cycle.guideline_id),
        terms_for(cycle),
        load_fx(db, cycle.fx_set_id),
        merit_minor=merit_minor,
        bonus_minor=bonus_minor,
    )
    if award["blockers"]:
        raise DomainError(
            422, "comp_award_blocked", "The award violates a hard limit or employment constraint."
        )
    membership = db.get(CompEligibility, (cycle.id, proposal.employee_id))
    if membership is None:
        raise FinanceInputError("COMPENSATION_ELIGIBILITY_UNAVAILABLE")
    pool_id = membership.pool_id
    pool = db.get(CompPool, pool_id)
    if pool is None:
        raise FinanceInputError("COMPENSATION_ELIGIBLE_POOL_UNAVAILABLE")
    other_awards = [
        revision.award
        for other, revision in latest_rows(db, cycle.id)
        if other.id != proposal.id
        and other.proposer_id == pool.manager_id
        and revision.award["org_id"] == pool.org_id
        and revision.award["currency"] == pool.currency
    ]
    utilization = evaluate_pool(
        Pool(
            pool.org_id,
            pool.currency,
            pool.cost_center,
            exact_decimal(pool.budget_minor),
            exact_decimal(pool.holdback_minor),
        ),
        other_awards + [award],
    )
    if utilization["blockers"]:
        raise DomainError(
            422, "comp_pool_budget_exceeded", "The proposed awards exceed the assigned pool budget."
        )
    revision = CompProposalRevision(
        id=uid(),
        proposal_id=proposal.id,
        number=proposal.current_revision + 1,
        kind=kind,
        merit_minor=Decimal(merit_minor),
        bonus_minor=Decimal(bonus_minor),
        comment=comment,
        reason=reason,
        author_id=author_id,
        award=award,
    )
    revision.sha256 = canonical_hash(revision_document(cycle, proposal, revision, frozen.sha256))
    db.add(revision)
    db.flush()
    assignment = next(row for row in cycle.approvals if row["org_id"] == frozen.org_id)
    for ordinal, role, field, capability in SERIAL_STEPS:
        db.add(
            CompApprovalStep(
                revision_id=revision.id,
                proposal_id=proposal.id,
                ordinal=ordinal,
                role=role,
                assigned_id=assignment[field],
                capability=capability,
                exception_codes=award["exceptions"] if ordinal == 1 else [],
            )
        )
    db.flush()
    proposal.current_revision = revision.number
    proposal.version += 1
    proposal.state = "RETURNED" if kind == "RETURN" else "DRAFT"
    db.flush()
    revision_event(db, cycle, proposal, revision, author_id, request_id)
    return revision


def launch_cycle(db: Session, cycle: CompCycle, *, reason: str, request_id: str) -> None:
    check_inputs(db, cycle)
    population = load_eligibility(
        db,
        snapshot_id=cycle.snapshot_id,
        guideline_id=cycle.guideline_id,
        terms=terms_for(cycle),
        selected_ids=tuple(cycle.selected_ids),
    )
    pools = {
        pool_key(pool): pool
        for pool in db.scalars(select(CompPool).where(CompPool.cycle_id == cycle.id))
    }
    keys = {(row.proposer_id, row.org_id, row.currency) for row in population if row.eligible}
    if not keys or keys != set(pools):
        raise DomainError(
            422,
            "comp_pool_assignment_mismatch",
            "Each eligible manager/currency/department requires exactly one configured pool.",
        )
    for frozen in population:
        pool = (
            pools[(frozen.proposer_id or "", frozen.org_id or "", frozen.currency or "")]
            if frozen.eligible
            else None
        )
        db.add(
            CompEligibility(
                cycle_id=cycle.id,
                employee_id=frozen.employee_id,
                eligible=frozen.eligible,
                proposer_id=frozen.proposer_id,
                pool_id=pool.id if pool else None,
                document=frozen.document(),
                sha256=frozen.sha256,
            )
        )
    db.flush()
    for frozen in population:
        if not frozen.eligible or frozen.proposer_id is None:
            continue
        proposal = CompProposal(
            cycle_id=cycle.id, employee_id=frozen.employee_id, proposer_id=frozen.proposer_id
        )
        db.add(proposal)
        db.flush()
        save_revision(
            db,
            cycle,
            proposal,
            merit_minor="0",
            bonus_minor="0",
            comment="",
            reason="Cycle launch; no award proposed",
            author_id=cycle.owner_id,
            kind="INITIAL",
            request_id=request_id,
        )
    record_cycle_action(
        db,
        cycle,
        action="LAUNCH",
        actor_id=cycle.owner_id,
        reason=reason,
        evidence=cycle.inputs_sha256,
        request_id=request_id,
        state="OPEN",
    )


def verify_revision(
    db: Session, proposal_id: str, revision_id: str, cycle_id: str, source_batch_id: str
) -> None:
    proposal = db.get(CompProposal, proposal_id)
    revision = db.get(CompProposalRevision, revision_id)
    cycle = db.get(CompCycle, cycle_id)
    snapshot = db.get(Snapshot, cycle.snapshot_id) if cycle else None
    if (
        proposal is None
        or revision is None
        or cycle is None
        or snapshot is None
        or proposal.cycle_id != cycle.id
        or revision.proposal_id != proposal.id
        or snapshot.source_batch_id != source_batch_id
    ):
        raise FinanceInputError("COMPENSATION_EVENT_SOURCE_BINDING_MISMATCH")
    check_inputs(db, cycle)
    expected = calculate_award(
        frozen_for(db, cycle, proposal.employee_id),
        load_guideline(db, cycle.guideline_id),
        terms_for(cycle),
        load_fx(db, cycle.fx_set_id),
        merit_minor=exact_decimal(revision.merit_minor),
        bonus_minor=exact_decimal(revision.bonus_minor),
    )
    if expected != checked_award(db, cycle, proposal, revision):
        raise FinanceInputError("COMPENSATION_INDEPENDENT_CALCULATION_MISMATCH")
    verified = db.get(CompRevisionVerification, revision.id)
    if verified is None:
        db.add(CompRevisionVerification(revision_id=revision.id, sha256=revision.sha256))
        db.flush()
    elif verified.sha256 != revision.sha256:
        raise FinanceInputError("COMPENSATION_VERIFICATION_FINGERPRINT_MISMATCH")
