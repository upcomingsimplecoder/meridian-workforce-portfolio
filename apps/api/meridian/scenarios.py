"""Scoped typed APIs; every mutation acknowledges one real PostgreSQL transaction."""

import csv
import io
from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import Select, or_, select, true, tuple_
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.finance_models import CostPolicyVersion, FxRateSet
from meridian.finance_repository import load_fx, load_policy
from meridian.governance import record
from meridian.idempotency import remember, replay
from meridian.identity import DB, Actor, CurrentActor, require
from meridian.models import OutboxEvent, Principal, RoleAssignment, now, uid
from meridian.people import published_snapshots, safe_cell
from meridian.people_models import OrgUnit, Snapshot
from meridian.scenario_models import (
    PlanningBaseline,
    Scenario,
    ScenarioDecision,
    ScenarioManifest,
    ScenarioRevision,
    ScenarioVerification,
)
from meridian.scenario_operations import OperationList, PlanningRules
from meridian.scenario_schemas import (
    AssumptionOption,
    BaselineView,
    BranchRequest,
    CompareItem,
    CompareQuery,
    CompareView,
    DecisionView,
    ForecastView,
    ManifestView,
    MoneyComponents,
    OperationsRequest,
    Option,
    ReasonRequest,
    ReviewerOption,
    ReviewRequest,
    RevisionQuery,
    RevisionView,
    ScenarioDetail,
    ScenarioOptions,
    ScenarioPage,
    ScenarioQuery,
    ScenarioRequest,
    ScenarioSummary,
)
from meridian.scenario_service import revision_for, save_revision, scenario_event, stored_forecast

router = APIRouter(prefix="/api/v1/scenarios", tags=["scenarios"])
Key = Annotated[UUID, Header(alias="Idempotency-Key")]
Match = Annotated[int, Header(alias="If-Match", ge=1)]


def atomic_command[**P, T](command: Callable[P, T]) -> Callable[P, T]:
    """Failed commands leave no draft, decision or partial revision behind."""

    @wraps(command)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        db = kwargs.get("db")
        if not isinstance(db, Session):
            raise RuntimeError("Scenario command requires its request transaction")
        with db.begin_nested():
            return command(*args, **kwargs)

    return wrapped


def scopes_for(current: Actor, capability: str) -> set[str]:
    return {
        grant.scope for grant in current.grants if capability in current.policy.get(grant.role, [])
    }


def deny(db: Session, current: Actor, request: Request, code: str, *, status: int = 403) -> None:
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
        status, code, "The requested scenario action is unavailable in your current scope."
    )


def check_scope(
    db: Session,
    current: Actor,
    request: Request,
    orgs: list[str],
    capability: str = "planning.write",
) -> None:
    require(current, capability, db, request)
    allowed = scopes_for(current, capability)
    if "*" not in allowed and not set(orgs) <= allowed:
        deny(db, current, request, "planning_scope_denied")


def base_query(current: Actor) -> Select[Scenario]:
    query = select(Scenario).where(
        Scenario.state != "BUILDING",
        or_(
            Scenario.owner_id == current.principal.id, Scenario.reviewer_id == current.principal.id
        ),
    )
    scopes = scopes_for(current, "planning.write")
    if "*" not in scopes:
        query = query.where(Scenario.scope_orgs.contained_by(sorted(scopes)))
    return query


def scenario_for(
    db: Session, current: Actor, request: Request, scenario_id: str, *, lock: bool = False
) -> Scenario:
    require(current, "planning.write", db, request)
    query = base_query(current).where(Scenario.id == scenario_id)
    if lock:
        query = query.with_for_update(of=Scenario)
    scenario = db.scalar(query)
    if scenario is None:
        deny(db, current, request, "scenario_unavailable", status=404)
        raise AssertionError("deny raises")
    return scenario


def reviewer_valid(
    db: Session, current: Actor, request: Request, reviewer_id: str, orgs: list[str]
) -> None:
    if reviewer_id == current.principal.id:
        deny(db, current, request, "self_review_denied")
    principal = db.get(Principal, reviewer_id)
    scopes = {
        grant.scope
        for grant in db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.principal_id == reviewer_id,
                RoleAssignment.effective_from <= now(),
                or_(RoleAssignment.effective_to.is_(None), RoleAssignment.effective_to > now()),
            )
        )
        if "planning.write" in current.policy.get(grant.role, [])
    }
    if principal is None or not principal.active or "*" not in scopes and not set(orgs) <= scopes:
        deny(db, current, request, "reviewer_scope_denied")


def summary(db: Session, scenario: Scenario) -> ScenarioSummary:
    revision = revision_for(db, scenario)
    forecast = stored_forecast(db, revision.id)
    jobs = list(
        db.scalars(
            select(OutboxEvent.state).where(
                OutboxEvent.aggregate_id == scenario.id, OutboxEvent.event_type.like("scenario.%")
            )
        )
    )
    result = forecast.document["result"]
    return ScenarioSummary(
        id=scenario.id,
        title=scenario.title,
        owner_id=scenario.owner_id,
        reviewer_id=scenario.reviewer_id,
        version=scenario.version,
        revision=scenario.current_revision,
        state=scenario.state,
        base_snapshot_id=scenario.base_snapshot_id,
        policy_id=scenario.policy_id,
        fx_set_id=scenario.fx_set_id,
        parent_scenario_id=scenario.parent_scenario_id,
        parent_revision_id=scenario.parent_revision_id,
        year=scenario.year,
        planning_date=scenario.planning_date,
        scope_orgs=scenario.scope_orgs,
        budget_minor=scenario.budget_minor,
        currency=result["currency"],
        exponent=result["exponent"],
        annual_total_minor=result["annual"]["total_minor"],
        forecast_sha256=forecast.sha256,
        verified=db.get(ScenarioVerification, revision.id) is not None,
        manifest_ready=db.get(ScenarioManifest, revision.id) is not None,
        pending_job="PENDING" in jobs,
        failed_job="DEAD" in jobs,
        created_at=scenario.created_at,
    )


def detail(db: Session, scenario: Scenario) -> ScenarioDetail:
    return ScenarioDetail(
        **summary(db, scenario).model_dump(),
        operations=OperationList.validate_python(revision_for(db, scenario).operations),
        rules=scenario.rules,
        revisions=[
            RevisionView.model_validate(row, from_attributes=True)
            for row in db.scalars(
                select(ScenarioRevision)
                .where(ScenarioRevision.scenario_id == scenario.id)
                .order_by(ScenarioRevision.number)
            )
        ],
        decisions=[
            DecisionView.model_validate(row, from_attributes=True)
            for row in db.scalars(
                select(ScenarioDecision)
                .where(ScenarioDecision.scenario_id == scenario.id)
                .order_by(ScenarioDecision.expected_version)
            )
        ],
    )


def finish(
    db: Session,
    current: Actor,
    request: Request,
    key: UUID,
    payload: dict[str, Any],
    scenario: Scenario,
) -> ScenarioDetail:
    response = detail(db, scenario)
    remember(
        db,
        current.principal.id,
        request.url.path,
        str(key),
        payload,
        response.model_dump(mode="json"),
        request.state.request_id,
    )
    db.commit()
    return response


def replay_detail(
    db: Session, current: Actor, request: Request, key: UUID, payload: dict[str, Any]
) -> ScenarioDetail | None:
    previous = replay(db, current.principal.id, request.url.path, str(key), payload)
    return ScenarioDetail.model_validate(previous) if previous else None


def owner_only(db: Session, current: Actor, request: Request, scenario: Scenario) -> None:
    if scenario.owner_id != current.principal.id:
        deny(db, current, request, "scenario_owner_required")


def match_version(scenario: Scenario, expected: int) -> None:
    if scenario.version != expected:
        raise DomainError(
            409,
            "stale_scenario",
            f"Scenario changed. Refresh revision; current version is {scenario.version}.",
        )


@router.get("/options", response_model=ScenarioOptions)
def options(
    current: CurrentActor,
    db: DB,
    request: Request,
    source_search: Annotated[str, Query(max_length=100)] = "",
) -> ScenarioOptions:
    require(current, "planning.write", db, request)
    allowed = scopes_for(current, "planning.write")
    orgs = list(
        db.scalars(
            select(OrgUnit)
            .where(OrgUnit.id.like("D%"), true() if "*" in allowed else OrgUnit.id.in_(allowed))
            .order_by(OrgUnit.id)
        )
    )
    reviewers = []
    for principal in db.scalars(
        select(Principal)
        .where(Principal.active.is_(True), Principal.id != current.principal.id)
        .limit(50)
    ):
        scopes = {
            grant.scope
            for grant in db.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.principal_id == principal.id,
                    RoleAssignment.effective_from <= now(),
                    or_(RoleAssignment.effective_to.is_(None), RoleAssignment.effective_to > now()),
                )
            )
            if "planning.write" in current.policy.get(grant.role, [])
        }
        if scopes:
            reviewers.append(
                ReviewerOption(
                    id=principal.id, label=principal.display_name, scope_orgs=sorted(scopes)
                )
            )
    return ScenarioOptions(
        snapshots=[
            Option(id=row.id, label=row.title) for row in published_snapshots(db, source_search)
        ],
        policies=[
            AssumptionOption(
                id=row.id, label=row.source_label, document=load_policy(db, row.id).document()
            )
            for row in db.scalars(
                select(CostPolicyVersion)
                .order_by(CostPolicyVersion.effective_date.desc())
                .limit(20)
            )
        ],
        fx_sets=[
            AssumptionOption(
                id=row.id, label=row.source_label, document=load_fx(db, row.id).document()
            )
            for row in db.scalars(
                select(FxRateSet)
                .where(FxRateSet.state == "SEALED")
                .order_by(FxRateSet.captured_at, FxRateSet.id)
                .limit(20)
            )
        ],
        reviewers=reviewers,
        scope_orgs=[Option(id=row.id, label=row.name) for row in orgs],
    )


@router.get("", response_model=ScenarioPage)
def list_scenarios(
    current: CurrentActor, db: DB, request: Request, query: Annotated[ScenarioQuery, Query()]
) -> ScenarioPage:
    require(current, "planning.write", db, request)
    statement = base_query(current)
    if query.search:
        search = query.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(Scenario.title.ilike(f"%{search}%", escape="\\"))
    if query.cursor:
        cursor = scenario_for(db, current, request, str(query.cursor))
        statement = statement.where(
            tuple_(Scenario.created_at, Scenario.id) < (cursor.created_at, cursor.id)
        )
    rows = list(
        db.scalars(
            statement.order_by(Scenario.created_at.desc(), Scenario.id.desc()).limit(
                query.limit + 1
            )
        )
    )
    return ScenarioPage(
        items=[summary(db, row) for row in rows[: query.limit]],
        next_cursor=rows[query.limit - 1].id if len(rows) > query.limit else None,
    )


@router.post("", response_model=ScenarioDetail, status_code=201)
@atomic_command
def create_scenario(
    body: ScenarioRequest, key: Key, current: CurrentActor, db: DB, request: Request
) -> ScenarioDetail:
    check_scope(db, current, request, body.scope_orgs)
    reviewer_valid(db, current, request, body.reviewer_id, body.scope_orgs)
    payload = body.model_dump(mode="json")
    previous = replay_detail(db, current, request, key, payload)
    if previous:
        return previous
    snapshot = db.get(Snapshot, str(body.base_snapshot_id))
    if snapshot is None or snapshot.status != "PUBLISHED":
        raise DomainError(422, "snapshot_unavailable", "Select a published snapshot.")
    load_policy(db, str(body.policy_id))
    load_fx(db, str(body.fx_set_id))
    scenario = Scenario(
        id=uid(),
        owner_id=current.principal.id,
        **body.model_dump(exclude={"scope_orgs"}),
        scope_orgs=sorted(body.scope_orgs),
        rules=PlanningRules().document(),
    )
    scenario.base_snapshot_id, scenario.policy_id, scenario.fx_set_id = (
        str(body.base_snapshot_id),
        str(body.policy_id),
        str(body.fx_set_id),
    )
    if len(set(body.scope_orgs)) != len(body.scope_orgs):
        raise DomainError(422, "duplicate_scope", "Select each department once.")
    db.add(scenario)
    db.flush()
    save_revision(
        db,
        scenario,
        [],
        current.principal.id,
        "Created pinned scenario baseline",
        request.state.request_id,
    )
    return finish(db, current, request, key, payload, scenario)


@router.get("/compare", response_model=CompareView)
def compare(
    current: CurrentActor, db: DB, request: Request, query: Annotated[CompareQuery, Query()]
) -> CompareView:
    scenarios = [scenario_for(db, current, request, str(id)) for id in query.ids]
    results = [
        stored_forecast(db, revision_for(db, row).id).document["result"] for row in scenarios
    ]
    first = results[0]
    if any(
        (
            result["currency"],
            result["exponent"],
            result["inputs"]["year"],
            result["inputs"]["scope_orgs"],
        )
        != (
            first["currency"],
            first["exponent"],
            first["inputs"]["year"],
            first["inputs"]["scope_orgs"],
        )
        for result in results
    ):
        raise DomainError(
            422,
            "incompatible_comparison",
            "Compare the same period, departments and reporting currency.",
        )
    return CompareView(
        items=[
            CompareItem(
                scenario=summary(db, row),
                result=result,
                annual_delta_minor=str(
                    int(result["annual"]["total_minor"]) - int(first["annual"]["total_minor"])
                ),
                monthly_delta_minor=[
                    str(int(m["total_minor"]) - int(b["total_minor"]))
                    for m, b in zip(result["months"], first["months"], strict=True)
                ],
            )
            for row, result in zip(scenarios, results, strict=True)
        ]
    )


@router.get("/baselines", response_model=list[BaselineView])
def baselines(current: CurrentActor, db: DB, request: Request) -> list[BaselineView]:
    require(current, "planning.write", db, request)
    ids = base_query(current).with_only_columns(Scenario.id)
    return [
        BaselineView.model_validate(row, from_attributes=True)
        for row in db.scalars(
            select(PlanningBaseline)
            .where(PlanningBaseline.scenario_id.in_(ids))
            .order_by(PlanningBaseline.created_at.desc())
            .limit(100)
        )
    ]


@router.get("/{scenario_id}", response_model=ScenarioDetail)
def get_scenario(
    scenario_id: UUID, current: CurrentActor, db: DB, request: Request
) -> ScenarioDetail:
    return detail(db, scenario_for(db, current, request, str(scenario_id)))


@router.get("/{scenario_id}/forecast", response_model=ForecastView)
def forecast(
    scenario_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    query: Annotated[RevisionQuery, Query()],
) -> ForecastView:
    scenario = scenario_for(db, current, request, str(scenario_id))
    revision = revision_for(db, scenario, query.revision)
    current_forecast = stored_forecast(db, revision.id)
    baseline = stored_forecast(db, scenario.parent_revision_id or revision_for(db, scenario, 1).id)
    planned, base = current_forecast.document["result"], baseline.document["result"]
    return ForecastView(
        current=current_forecast.document,
        baseline=baseline.document,
        manifest_sha256=current_forecast.sha256,
        annual_delta_minor=str(
            int(planned["annual"]["total_minor"]) - int(base["annual"]["total_minor"])
        ),
        annual_component_delta=MoneyComponents(
            **{
                component: str(int(value) - int(base["annual"][component]))
                for component, value in planned["annual"].items()
            }
        ),
        monthly_delta_minor=[
            str(int(m["total_minor"]) - int(b["total_minor"]))
            for m, b in zip(planned["months"], base["months"], strict=True)
        ],
        verified=db.get(ScenarioVerification, revision.id) is not None,
    )


@router.post("/{scenario_id}/branches", response_model=ScenarioDetail, status_code=201)
@atomic_command
def branch(
    scenario_id: UUID,
    body: BranchRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    parent = scenario_for(db, current, request, str(scenario_id), lock=True)
    reviewer_valid(db, current, request, body.reviewer_id, parent.scope_orgs)
    payload = body.model_dump(mode="json") | {"version": match}
    previous = replay_detail(db, current, request, key, payload)
    if previous:
        return previous
    match_version(parent, match)
    parent_revision = revision_for(db, parent)
    child = Scenario(
        id=uid(),
        title=body.title,
        owner_id=current.principal.id,
        reviewer_id=body.reviewer_id,
        base_snapshot_id=parent.base_snapshot_id,
        policy_id=parent.policy_id,
        fx_set_id=parent.fx_set_id,
        parent_scenario_id=parent.id,
        parent_revision_id=parent_revision.id,
        year=parent.year,
        planning_date=parent.planning_date,
        scope_orgs=parent.scope_orgs,
        rules=parent.rules,
        budget_minor=parent.budget_minor,
    )
    db.add(child)
    db.flush()
    save_revision(
        db,
        child,
        parent_revision.operations,
        current.principal.id,
        body.reason,
        request.state.request_id,
    )
    return finish(db, current, request, key, payload, child)


@router.post("/{scenario_id}/operations", response_model=ScenarioDetail)
@atomic_command
def operations(
    scenario_id: UUID,
    body: OperationsRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    scenario = scenario_for(db, current, request, str(scenario_id), lock=True)
    owner_only(db, current, request, scenario)
    payload = body.model_dump(mode="json") | {"version": match}
    previous = replay_detail(db, current, request, key, payload)
    if previous:
        return previous
    match_version(scenario, match)
    if scenario.state not in {"DRAFT", "REJECTED"}:
        raise DomainError(
            409, "scenario_locked", "Create a branch to change a submitted or approved plan."
        )
    save_revision(
        db,
        scenario,
        revision_for(db, scenario).operations + body.model_dump(mode="json")["operations"],
        current.principal.id,
        body.reason,
        request.state.request_id,
    )
    return finish(db, current, request, key, payload, scenario)


@router.post("/{scenario_id}/undo", response_model=ScenarioDetail)
@atomic_command
def undo(
    scenario_id: UUID,
    body: ReasonRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    scenario = scenario_for(db, current, request, str(scenario_id), lock=True)
    owner_only(db, current, request, scenario)
    payload = body.model_dump(mode="json") | {"version": match}
    previous = replay_detail(db, current, request, key, payload)
    if previous:
        return previous
    match_version(scenario, match)
    original = revision_for(db, scenario).operations
    if scenario.state not in {"DRAFT", "REJECTED"} or not original:
        raise DomainError(
            409, "undo_unavailable", "Undo requires an editable plan with an operation."
        )
    save_revision(
        db, scenario, original[:-1], current.principal.id, body.reason, request.state.request_id
    )
    return finish(db, current, request, key, payload, scenario)


def transition(
    db: Session,
    current: Actor,
    request: Request,
    key: UUID,
    match: int,
    scenario_id: UUID,
    body: ReasonRequest,
    action: str,
    source_state: str,
    target_state: str,
) -> ScenarioDetail:
    scenario = scenario_for(db, current, request, str(scenario_id), lock=True)
    if action == "SUBMIT":
        owner_only(db, current, request, scenario)
        reviewer_valid(db, current, request, scenario.reviewer_id, scenario.scope_orgs)
    elif current.principal.id != scenario.reviewer_id or current.principal.id == scenario.owner_id:
        deny(db, current, request, "assigned_reviewer_required")
    payload = body.model_dump(mode="json") | {"version": match, "action": action}
    previous = replay_detail(db, current, request, key, payload)
    if previous:
        return previous
    match_version(scenario, match)
    if scenario.state != source_state:
        raise DomainError(
            409, "invalid_scenario_state", "Refresh the plan before making this decision."
        )
    revision = revision_for(db, scenario)
    if (
        action in {"SUBMIT", "APPROVE", "PROMOTE"}
        and db.get(ScenarioVerification, revision.id) is None
    ):
        raise DomainError(
            409, "verification_pending", "Independent worker verification must finish first."
        )
    reason = body.reason.strip()
    if len(reason) < 3:
        raise DomainError(422, "reason_required", "Enter a meaningful decision reason.")
    manifest = db.get(ScenarioManifest, revision.id) if action == "PROMOTE" else None
    if action == "PROMOTE" and manifest is None:
        raise DomainError(
            409,
            "manifest_pending",
            "The worker must finish the approved manifest before promotion.",
        )
    decision = ScenarioDecision(
        id=uid(),
        scenario_id=scenario.id,
        revision_id=revision.id,
        actor_id=current.principal.id,
        expected_version=scenario.version,
        action=action,
        reason=reason,
        request_id=request.state.request_id,
    )
    db.add(decision)
    db.flush()
    if action == "PROMOTE":
        if manifest is None:
            raise DomainError(409, "manifest_pending", "The approved manifest is unavailable.")
        db.add(
            PlanningBaseline(
                scenario_id=scenario.id,
                revision_id=revision.id,
                decision_id=decision.id,
                manifest_sha256=manifest.sha256,
            )
        )
        db.flush()
    scenario.state, scenario.version = target_state, scenario.version + 1
    db.flush()
    if action == "APPROVE":
        scenario_event(
            db,
            scenario,
            revision,
            current.principal.id,
            "scenario.approved",
            request.state.request_id,
        )
    else:
        record(
            db,
            current.principal.id,
            f"scenario.{action.lower()}",
            "scenario_decision",
            decision.id,
            request.state.request_id,
            after_hash=revision.sha256,
            emit=False,
            entity_version=scenario.version,
        )
    return finish(db, current, request, key, payload, scenario)


@router.post("/{scenario_id}/submit", response_model=ScenarioDetail)
@atomic_command
def submit(
    scenario_id: UUID,
    body: ReasonRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    return transition(
        db, current, request, key, match, scenario_id, body, "SUBMIT", "DRAFT", "SUBMITTED"
    )


@router.post("/{scenario_id}/review", response_model=ScenarioDetail)
@atomic_command
def start_review(
    scenario_id: UUID,
    body: ReasonRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    return transition(
        db,
        current,
        request,
        key,
        match,
        scenario_id,
        body,
        "START_REVIEW",
        "SUBMITTED",
        "IN_REVIEW",
    )


@router.post("/{scenario_id}/decisions", response_model=ScenarioDetail)
@atomic_command
def decision(
    scenario_id: UUID,
    body: ReviewRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    return transition(
        db,
        current,
        request,
        key,
        match,
        scenario_id,
        body,
        "APPROVE" if body.decision == "approve" else "RETURN",
        "IN_REVIEW",
        "APPROVED" if body.decision == "approve" else "REJECTED",
    )


@router.post("/{scenario_id}/promote", response_model=ScenarioDetail)
@atomic_command
def promote(
    scenario_id: UUID,
    body: ReasonRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    return transition(
        db, current, request, key, match, scenario_id, body, "PROMOTE", "APPROVED", "PROMOTED"
    )


@router.get("/{scenario_id}/manifest", response_model=ManifestView)
def manifest(scenario_id: UUID, current: CurrentActor, db: DB, request: Request) -> ManifestView:
    scenario = scenario_for(db, current, request, str(scenario_id))
    stored = db.get(ScenarioManifest, revision_for(db, scenario).id)
    if stored is None:
        raise DomainError(
            409, "manifest_pending", "No approved manifest is available for this revision."
        )
    return ManifestView(sha256=stored.sha256, document=stored.document)


@router.post("/{scenario_id}/retry", response_model=ScenarioDetail)
@atomic_command
def retry_jobs(
    scenario_id: UUID,
    body: ReasonRequest,
    key: Key,
    match: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> ScenarioDetail:
    scenario = scenario_for(db, current, request, str(scenario_id), lock=True)
    payload = body.model_dump(mode="json") | {"version": match}
    previous = replay_detail(db, current, request, key, payload)
    if previous:
        return previous
    match_version(scenario, match)
    if len(body.reason.strip()) < 3:
        raise DomainError(422, "reason_required", "Enter a meaningful retry reason.")
    events = list(
        db.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == scenario.id,
                OutboxEvent.event_type.like("scenario.%"),
                OutboxEvent.state == "DEAD",
            )
            .with_for_update()
        )
    )
    if not events:
        raise DomainError(409, "retry_unavailable", "No failed scenario jobs require retry.")
    for event in events:
        event.state, event.attempts, event.last_error, event.next_attempt_at = (
            "PENDING",
            0,
            None,
            now(),
        )
    record(
        db,
        current.principal.id,
        "scenario.jobs_retried",
        "scenario",
        scenario.id,
        request.state.request_id,
        emit=False,
        entity_version=scenario.version,
    )
    return finish(db, current, request, key, payload, scenario)


@router.get("/{scenario_id}/export", response_class=Response)
def export(
    scenario_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    query: Annotated[RevisionQuery, Query()],
) -> Response:
    scenario = scenario_for(db, current, request, str(scenario_id))
    check_scope(db, current, request, scenario.scope_orgs, "people.export")
    revision = revision_for(db, scenario, query.revision)
    stored = stored_forecast(db, revision.id)
    result = stored.document["result"]
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        [
            "scenario_id",
            "revision",
            "forecast_sha256",
            "snapshot_id",
            "policy_id",
            "fx_set_id",
            "currency",
            "exponent",
            "month",
            "org_id",
            "closing_headcount",
            "average_fte",
            "base_minor",
            "benefits_minor",
            "variable_minor",
            "employer_tax_minor",
            "total_minor",
        ]
    )
    for month in result["months"]:
        for org in month["departments"]:
            writer.writerow(
                [
                    safe_cell(str(value))
                    for value in [
                        scenario.id,
                        revision.number,
                        stored.sha256,
                        scenario.base_snapshot_id,
                        scenario.policy_id,
                        scenario.fx_set_id,
                        result["currency"],
                        result["exponent"],
                        month["month"],
                        org["org_id"],
                        org["closing_headcount"],
                        org["average_fte"],
                        org["base_minor"],
                        org["benefits_minor"],
                        org["variable_minor"],
                        org["employer_tax_minor"],
                        org["total_minor"],
                    ]
                ]
            )
    content = stream.getvalue().encode()
    if len(content) > 1024 * 1024:
        raise DomainError(413, "export_size_limit", "Export exceeds the bounded download size.")
    record(
        db,
        current.principal.id,
        "scenario.export",
        "scenario_revision",
        revision.id,
        request.state.request_id,
        after_hash=stored.sha256,
        emit=False,
        entity_version=revision.number,
    )
    db.commit()
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="meridian-scenario-{scenario.id}-r{revision.number}.csv"',
            "Cache-Control": "no-store",
        },
    )
