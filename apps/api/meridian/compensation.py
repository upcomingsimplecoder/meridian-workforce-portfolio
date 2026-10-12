"""Typed, currently authorized compensation APIs backed by guarded PostgreSQL state."""

import json
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from meridian.commands import audited_command as compensation_command
from meridian.compensation_approvals import (
    decide,
    lock_cycle,
    next_step,
    start_review,
    submit_proposal,
)
from meridian.compensation_auth import (
    READ_CAPABILITIES,
    covers,
    full_cycle_access,
    scopes,
    validate_assignments,
    visible_proposals,
)
from meridian.compensation_exports import reconcile_export, verified_artifact
from meridian.compensation_models import (
    CompApprovalStep,
    CompCycle,
    CompEligibility,
    CompGuidelineVersion,
    CompProposal,
)
from meridian.compensation_projection import cycle_view, evidence_view, proposal_view
from meridian.compensation_schemas import (
    AwardRequest,
    CompEvidencePage,
    CompEvidenceQuery,
    CompEvidenceView,
    CompExportView,
    CompOption,
    CompOptions,
    CompQuery,
    CompReason,
    CompReview,
    CompReviewStart,
    CompRevisionQuery,
    CycleCreate,
    CyclePage,
    CycleView,
    EligibilityPage,
    EligibilityQuery,
    EligibilityView,
    ProposalPage,
    ProposalView,
)
from meridian.compensation_service import (
    launch_cycle,
    prepare_cycle,
    record_cycle_action,
    revision_for,
    save_revision,
)
from meridian.errors import DomainError
from meridian.finance_models import FxRateSet
from meridian.governance import record
from meridian.idempotency import remember, replay
from meridian.identity import DB, Actor, CurrentActor, require
from meridian.models import OutboxEvent, Principal, RoleAssignment
from meridian.object_store import read_private, sign_link, verify_link
from meridian.people import published_snapshots
from meridian.policy import POLICY_ID

router = APIRouter(prefix="/api/v1/compensation", tags=["compensation"])
Key = Annotated[UUID, Header(alias="Idempotency-Key")]
Match = Annotated[int, Header(alias="If-Match", ge=1)]


def deny(db: Session, current: Actor, request: Request, code: str, status: int = 403) -> NoReturn:
    record(
        db,
        current.principal.id,
        f"security.{code}",
        "route",
        request.url.path,
        request.state.request_id,
        False,
        emit=False,
    )
    db.commit()
    raise DomainError(
        status, code, "This compensation resource or action is unavailable in your current scope."
    )


def require_raw(db: Session, current: Actor, request: Request) -> None:
    require(current, "people.pay", db, request)
    if not current.capabilities.intersection(READ_CAPABILITIES):
        require(current, "comp.read", db, request)


def cycle_for(
    db: Session, current: Actor, request: Request, cycle_id: str, *, lock: bool = False
) -> CompCycle:
    require_raw(db, current, request)
    query = select(CompCycle).where(CompCycle.id == cycle_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    cycle = db.scalar(query)
    if (
        cycle is None
        or not full_cycle_access(current, cycle)
        and db.scalar(visible_proposals(db, current, cycle).limit(1)) is None
    ):
        deny(db, current, request, "comp_cycle_unavailable", 404)
    return cycle


def proposal_for(
    db: Session, current: Actor, request: Request, proposal_id: str, *, lock: bool = False
) -> tuple[CompCycle, CompProposal]:
    require_raw(db, current, request)
    parent = db.scalar(select(CompProposal.cycle_id).where(CompProposal.id == proposal_id))
    if parent is None:
        deny(db, current, request, "comp_proposal_unavailable", 404)
    cycle = cycle_for(db, current, request, parent, lock=lock)
    query = visible_proposals(db, current, cycle).where(CompProposal.id == proposal_id)
    if lock:
        query = query.with_for_update(of=CompProposal).execution_options(populate_existing=True)
    proposal = db.scalar(query)
    if proposal is None:
        deny(db, current, request, "comp_proposal_unavailable", 404)
    return cycle, proposal


def controller(db: Session, current: Actor, request: Request, cycle: CompCycle) -> None:
    require(current, "comp.manage", db, request)
    if (
        cycle.owner_id != current.principal.id
        or not covers(current, "comp.manage", cycle.scope_orgs)
        or not covers(current, "people.pay", cycle.scope_orgs)
    ):
        deny(db, current, request, "comp_controller_required")


def matching(actual: int, expected: int) -> None:
    if actual != expected:
        raise DomainError(
            409,
            "comp_version_conflict",
            "The record changed. Reload the acknowledged version before trying again.",
        )


def payload(body: BaseModel, expected: int | None = None) -> dict[str, Any]:
    return body.model_dump(mode="json") | (
        {"expected_version": expected} if expected is not None else {}
    )


def finish[T: BaseModel](
    db: Session, current: Actor, request: Request, key: UUID, values: dict[str, Any], response: T
) -> T:
    remember(
        db,
        current.principal.id,
        request.url.path,
        str(key),
        values,
        response.model_dump(mode="json"),
        request.state.request_id,
    )
    db.commit()
    return response


@router.get("/options", response_model=CompOptions)
def options(
    current: CurrentActor,
    db: DB,
    request: Request,
    source_search: Annotated[str, Query(max_length=100)] = "",
) -> CompOptions:
    require(current, "comp.manage", db, request)
    require(current, "people.pay", db, request)
    from meridian.models import now

    grants = list(
        db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.effective_from <= now(),
                or_(RoleAssignment.effective_to.is_(None), RoleAssignment.effective_to > now()),
            )
        )
    )
    principals = list(
        db.scalars(select(Principal).where(Principal.active.is_(True)).order_by(Principal.id))
    )

    def candidates(capability: str) -> list[CompOption]:
        return [
            CompOption(
                id=principal.id,
                label=principal.display_name,
                scopes=sorted(
                    {
                        grant.scope
                        for grant in grants
                        if grant.principal_id == principal.id
                        and capability in current.policy.get(grant.role, [])
                    }
                ),
            )
            for principal in principals
            if any(
                grant.principal_id == principal.id
                and capability in current.policy.get(grant.role, [])
                for grant in grants
            )
        ]

    return CompOptions(
        snapshots=[
            CompOption(id=row.id, label=row.title) for row in published_snapshots(db, source_search)
        ],
        guidelines=[
            CompOption(id=row.id, label=row.source_label)
            for row in db.scalars(
                select(CompGuidelineVersion)
                .order_by(CompGuidelineVersion.effective_date, CompGuidelineVersion.id)
                .limit(100)
            )
        ],
        fx_sets=[
            CompOption(id=row.id, label=row.source_label)
            for row in db.scalars(
                select(FxRateSet)
                .where(FxRateSet.state == "SEALED")
                .order_by(FxRateSet.id)
                .limit(100)
            )
        ],
        managers=candidates("comp.propose"),
        division_approvers=candidates("comp.approve"),
        hr_signers=candidates("comp.hr.signoff"),
        finance_signers=candidates("comp.finance.signoff"),
    )


@router.get("/cycles", response_model=CyclePage)
def cycles(
    current: CurrentActor, db: DB, request: Request, filters: Annotated[CompQuery, Query()]
) -> CyclePage:
    require_raw(db, current, request)
    query = select(CompCycle).order_by(CompCycle.id)
    if filters.cursor:
        query = query.where(CompCycle.id > str(filters.cursor))
    if filters.search:
        query = query.where(CompCycle.title.icontains(filters.search, autoescape=True))
    all_scope = scopes(current, "comp.manage") | scopes(current, "comp.read")
    assigned = select(CompApprovalStep.proposal_id).where(
        CompApprovalStep.assigned_id == current.principal.id
    )
    own_cycles = select(CompProposal.cycle_id).where(
        or_(CompProposal.proposer_id == current.principal.id, CompProposal.id.in_(assigned))
    )
    if "*" not in all_scope:
        query = query.where(
            or_(CompCycle.scope_orgs.contained_by(sorted(all_scope)), CompCycle.id.in_(own_cycles))
        )
    # Query up to one bounded candidate page; revoked partial resources are filtered
    # with current object/field predicates and pagination advances past those candidates.
    candidates = list(db.scalars(query.limit(filters.limit + 1)))
    visible = [
        row
        for row in candidates[: filters.limit]
        if full_cycle_access(current, row)
        or db.scalar(visible_proposals(db, current, row).limit(1)) is not None
    ]
    return CyclePage(
        items=[cycle_view(db, current, row) for row in visible],
        next_cursor=candidates[filters.limit - 1].id if len(candidates) > filters.limit else None,
    )


@router.post("/cycles", response_model=CycleView)
@compensation_command
def create_cycle(
    body: CycleCreate, current: CurrentActor, db: DB, request: Request, key: Key
) -> CycleView:
    require(current, "comp.manage", db, request)
    require(current, "people.pay", db, request)
    values = payload(body)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        # A receipt may contain every configured pool. Reading one currently
        # visible proposal does not authorize that historical full response.
        controller(db, current, request, cycle_for(db, current, request, prior["id"]))
        return CycleView.model_validate_json(json.dumps(prior))
    cycle, pools, population = prepare_cycle(db, body, current.principal.id, POLICY_ID)
    all_orgs = sorted({period.org_id for row in population for period in row.history})
    if not covers(current, "comp.manage", all_orgs) or not covers(current, "people.pay", all_orgs):
        deny(db, current, request, "comp_configuration_scope_denied")
    validate_assignments(db, cycle, population)
    if any(row.proposer_id == current.principal.id for row in population if row.eligible):
        deny(db, current, request, "comp_self_finalize_denied")
    db.add(cycle)
    db.flush()
    db.add_all(pools)
    db.flush()
    record(
        db,
        current.principal.id,
        "comp.cycle_configured",
        "comp_cycle",
        cycle.id,
        request.state.request_id,
        after_hash=cycle.inputs_sha256,
        emit=False,
    )
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.get("/cycles/{cycle_id}", response_model=CycleView)
def cycle_detail(cycle_id: UUID, current: CurrentActor, db: DB, request: Request) -> CycleView:
    return cycle_view(db, current, cycle_for(db, current, request, str(cycle_id)))


@router.post("/cycles/{cycle_id}/launch", response_model=CycleView)
@compensation_command
def launch(
    cycle_id: UUID,
    body: CompReason,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> CycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), lock=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return CycleView.model_validate_json(json.dumps(prior))
    matching(cycle.version, expected)
    if cycle.state != "CONFIGURING":
        raise DomainError(
            409, "comp_cycle_already_launched", "This cycle configuration is already sealed."
        )
    from meridian.compensation_repository import load_eligibility
    from meridian.compensation_service import terms_for

    population = load_eligibility(
        db,
        snapshot_id=cycle.snapshot_id,
        guideline_id=cycle.guideline_id,
        terms=terms_for(cycle),
        selected_ids=tuple(cycle.selected_ids),
    )
    validate_assignments(db, cycle, population)
    launch_cycle(db, cycle, reason=body.reason, request_id=request.state.request_id)
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.get("/cycles/{cycle_id}/eligibility", response_model=EligibilityPage)
def eligibility(
    cycle_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[EligibilityQuery, Query()],
) -> EligibilityPage:
    cycle = cycle_for(db, current, request, str(cycle_id))
    require(current, "comp.manage", db, request)
    if not full_cycle_access(current, cycle):
        deny(db, current, request, "comp_eligibility_scope_denied")
    query = select(CompEligibility).where(CompEligibility.cycle_id == cycle.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if filters.cursor:
        query = query.where(CompEligibility.employee_id > filters.cursor)
    rows = list(db.scalars(query.order_by(CompEligibility.employee_id).limit(filters.limit + 1)))
    return EligibilityPage(
        items=[
            EligibilityView(
                employee_id=row.employee_id,
                eligible=row.eligible,
                proposer_id=row.proposer_id,
                reason_codes=row.document["reason_codes"],
                sha256=row.sha256,
            )
            for row in rows[: filters.limit]
        ],
        total=total,
        next_cursor=rows[filters.limit - 1].employee_id if len(rows) > filters.limit else None,
    )


@router.get("/cycles/{cycle_id}/proposals", response_model=ProposalPage)
def proposals(
    cycle_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[CompQuery, Query()],
) -> ProposalPage:
    cycle = cycle_for(db, current, request, str(cycle_id))
    query = visible_proposals(db, current, cycle)
    if filters.search:
        query = query.where(CompProposal.employee_id.icontains(filters.search, autoescape=True))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if filters.cursor:
        query = query.where(CompProposal.id > str(filters.cursor))
    rows = list(db.scalars(query.order_by(CompProposal.id).limit(filters.limit + 1)))
    return ProposalPage(
        items=[proposal_view(db, cycle, row, history=False) for row in rows[: filters.limit]],
        total=total,
        next_cursor=rows[filters.limit - 1].id if len(rows) > filters.limit else None,
    )


@router.get("/proposals/{proposal_id}", response_model=ProposalView)
def proposal_detail(
    proposal_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[CompRevisionQuery, Query()],
) -> ProposalView:
    cycle, proposal = proposal_for(db, current, request, str(proposal_id))
    return proposal_view(db, cycle, proposal, number=filters.revision)


@router.post("/proposals/{proposal_id}/award", response_model=ProposalView)
@compensation_command
def save_award(
    proposal_id: UUID,
    body: AwardRequest,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> ProposalView:
    cycle, proposal = proposal_for(db, current, request, str(proposal_id), lock=True)
    require(current, "comp.propose", db, request)
    if proposal.proposer_id != current.principal.id:
        deny(db, current, request, "comp_proposer_required")
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return ProposalView.model_validate(prior)
    matching(proposal.version, expected)
    if proposal.state not in {"DRAFT", "RETURNED"} or cycle.state not in {"OPEN", "REVIEWING"}:
        raise DomainError(409, "comp_proposal_closed", "This proposal is closed to manager edits.")
    save_revision(
        db,
        cycle,
        proposal,
        merit_minor=body.merit_minor,
        bonus_minor=body.bonus_minor,
        comment=body.comment,
        reason=body.reason,
        author_id=current.principal.id,
        kind="EDIT",
        request_id=request.state.request_id,
    )
    return finish(db, current, request, key, values, proposal_view(db, cycle, proposal))


@router.post("/proposals/{proposal_id}/submit", response_model=ProposalView)
@compensation_command
def submit(
    proposal_id: UUID,
    body: CompReason,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> ProposalView:
    cycle, proposal = proposal_for(db, current, request, str(proposal_id), lock=True)
    require(current, "comp.propose", db, request)
    if proposal.proposer_id != current.principal.id:
        deny(db, current, request, "comp_proposer_required")
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return ProposalView.model_validate(prior)
    matching(proposal.version, expected)
    submit_proposal(db, cycle, proposal, reason=body.reason, request_id=request.state.request_id)
    return finish(db, current, request, key, values, proposal_view(db, cycle, proposal))


@router.post("/proposals/{proposal_id}/review", response_model=ProposalView)
@compensation_command
def begin_review(
    proposal_id: UUID,
    body: CompReviewStart,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> ProposalView:
    cycle, proposal = proposal_for(db, current, request, str(proposal_id), lock=True)
    require(current, "comp.approve", db, request)
    step = db.get(CompApprovalStep, (revision_for(db, proposal).id, 1))
    if proposal.proposer_id == current.principal.id:
        deny(db, current, request, "comp_self_approval_denied")
    if step is None or step.assigned_id != current.principal.id:
        deny(db, current, request, "comp_assigned_reviewer_required")
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return ProposalView.model_validate(prior)
    matching(proposal.version, expected)
    if str(body.revision_id) != revision_for(db, proposal).id:
        raise DomainError(
            409, "comp_revision_conflict", "Review requires the exact current proposal revision."
        )
    start_review(
        db,
        cycle,
        proposal,
        actor_id=current.principal.id,
        reason=body.reason,
        request_id=request.state.request_id,
    )
    return finish(db, current, request, key, values, proposal_view(db, cycle, proposal))


@router.post("/proposals/{proposal_id}/decisions", response_model=ProposalView)
@compensation_command
def review_decision(
    proposal_id: UUID,
    body: CompReview,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> ProposalView:
    cycle, proposal = proposal_for(db, current, request, str(proposal_id), lock=True)
    if proposal.proposer_id == current.principal.id:
        deny(db, current, request, "comp_self_approval_denied")
    assigned = list(
        db.scalars(
            select(CompApprovalStep).where(
                CompApprovalStep.revision_id == revision_for(db, proposal).id,
                CompApprovalStep.assigned_id == current.principal.id,
            )
        )
    )
    if not assigned or not any(step.capability in current.capabilities for step in assigned):
        deny(db, current, request, "comp_assigned_reviewer_required")
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return ProposalView.model_validate(prior)
    matching(proposal.version, expected)
    if str(body.revision_id) != revision_for(db, proposal).id:
        raise DomainError(
            409, "comp_revision_conflict", "Decision requires the exact current proposal revision."
        )
    step = next_step(db, cycle, proposal)
    if step is None or step.assigned_id != current.principal.id:
        deny(db, current, request, "comp_serial_step_required")
    require(current, step.capability, db, request)
    decide(
        db,
        cycle,
        proposal,
        actor_id=current.principal.id,
        action=body.decision.upper(),
        reason=body.reason,
        exceptions=list[str](body.acknowledged_exceptions),
        request_id=request.state.request_id,
    )
    return finish(db, current, request, key, values, proposal_view(db, cycle, proposal))


@router.post("/cycles/{cycle_id}/lock", response_model=CycleView)
@compensation_command
def finalize(
    cycle_id: UUID,
    body: CompReason,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> CycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), lock=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return CycleView.model_validate_json(json.dumps(prior))
    matching(cycle.version, expected)
    lock_cycle(db, cycle, reason=body.reason, request_id=request.state.request_id)
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


def export_access(db: Session, current: Actor, request: Request, cycle: CompCycle) -> None:
    for capability in ("comp.export", "people.export", "people.pay"):
        require(current, capability, db, request)
        if not covers(current, capability, cycle.scope_orgs):
            deny(db, current, request, "comp_export_scope_denied")


@router.get("/cycles/{cycle_id}/export", response_model=CompExportView)
def export(cycle_id: UUID, current: CurrentActor, db: DB, request: Request) -> CompExportView:
    cycle = cycle_for(db, current, request, str(cycle_id))
    export_access(db, current, request, cycle)
    artifact = verified_artifact(db, cycle)
    return CompExportView(
        cycle_id=cycle.id,
        version=artifact.version,
        manifest_sha256=artifact.sha256,
        csv_sha256=artifact.csv_key,
        row_count=artifact.row_count,
        document=artifact.document,
        download_token=sign_link(
            current.principal.id + ":" + current.session.token_hash,
            "comp-export",
            cycle.id,
            artifact.version,
        ),
        expires_in_seconds=300,
    )


@router.get("/cycles/{cycle_id}/download")
def download(
    cycle_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    token: Annotated[str, Query(min_length=1, max_length=4096)],
) -> Response:
    cycle = cycle_for(db, current, request, str(cycle_id))
    export_access(db, current, request, cycle)
    artifact = verified_artifact(db, cycle)
    verify_link(
        token,
        current.principal.id + ":" + current.session.token_hash,
        "comp-export",
        cycle.id,
        artifact.version,
    )
    return Response(
        read_private(artifact.csv_key),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="meridian-comp-{cycle.id[:8]}-v{artifact.version}.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/cycles/{cycle_id}/reconcile", response_model=CycleView)
@compensation_command
def reconcile(
    cycle_id: UUID,
    body: CompReason,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> CycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), lock=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return CycleView.model_validate_json(json.dumps(prior))
    matching(cycle.version, expected)
    reconcile_export(db, cycle, reason=body.reason, request_id=request.state.request_id)
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.post("/cycles/{cycle_id}/retry-export", response_model=CycleView)
@compensation_command
def retry_export(
    cycle_id: UUID,
    body: CompReason,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Match,
) -> CycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), lock=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior:
        return CycleView.model_validate_json(json.dumps(prior))
    matching(cycle.version, expected)
    jobs = list(
        db.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == cycle.id,
                OutboxEvent.event_type == "comp.cycle_locked.v1",
                OutboxEvent.state == "DEAD",
            )
            .with_for_update()
        )
    )
    if cycle.state != "FAILED_EXPORT" or len(jobs) != 1:
        raise DomainError(
            409, "comp_export_retry_unavailable", "A failed export job is required before retrying."
        )
    from meridian.compensation_models import CompLock
    from meridian.models import now

    locked = db.scalar(select(CompLock).where(CompLock.cycle_id == cycle.id))
    if locked is None:
        raise DomainError(
            503, "comp_lock_unavailable", "The immutable lock evidence is unavailable."
        )
    record_cycle_action(
        db,
        cycle,
        action="RETRY_EXPORT",
        actor_id=current.principal.id,
        reason=body.reason,
        evidence=locked.sha256,
        request_id=request.state.request_id,
        state="LOCKED",
    )
    jobs[0].state = "PENDING"
    jobs[0].attempts = 0
    jobs[0].last_error = None
    jobs[0].next_attempt_at = now()
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.get("/evidence", response_model=CompEvidencePage)
def evidence_list(
    current: CurrentActor, db: DB, request: Request, filters: Annotated[CompEvidenceQuery, Query()]
) -> CompEvidencePage:
    require(current, "comp.evidence", db, request)
    query = select(CompCycle).order_by(CompCycle.id)
    allowed = scopes(current, "comp.evidence")
    if "*" not in allowed:
        query = query.where(CompCycle.scope_orgs.contained_by(sorted(allowed)))
    if filters.cursor:
        query = query.where(CompCycle.id > str(filters.cursor))
    rows = list(db.scalars(query.limit(filters.limit + 1)))
    return CompEvidencePage(
        items=[evidence_view(db, row) for row in rows[: filters.limit]],
        next_cursor=rows[filters.limit - 1].id if len(rows) > filters.limit else None,
    )


@router.get("/cycles/{cycle_id}/evidence", response_model=CompEvidenceView)
def evidence(cycle_id: UUID, current: CurrentActor, db: DB, request: Request) -> CompEvidenceView:
    require(current, "comp.evidence", db, request)
    cycle = db.get(CompCycle, str(cycle_id))
    if cycle is None or not covers(current, "comp.evidence", cycle.scope_orgs):
        deny(db, current, request, "comp_evidence_unavailable", 404)
    return evidence_view(db, cycle)
