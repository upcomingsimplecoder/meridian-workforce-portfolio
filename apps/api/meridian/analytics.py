"""Governed analytical API; no arbitrary SQL or raw Parquet downloads."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from meridian.analytics_auth import authorize
from meridian.analytics_models import AnalyticsPublication, AnalyticsRun
from meridian.analytics_pipeline import canonical_bytes, verified_result
from meridian.analytics_projection import evidence, newest_source, view
from meridian.analytics_schemas import (
    AnalyticsChoice,
    AnalyticsEvidence,
    AnalyticsGapRequest,
    AnalyticsList,
    AnalyticsOptions,
    AnalyticsPublic,
    AnalyticsRequest,
    AnalyticsRunView,
)
from meridian.analytics_service import create_run, derived_request, retry_run
from meridian.commands import audited_command
from meridian.compensation_models import CompCycle, CompExportArtifact
from meridian.errors import DomainError
from meridian.governance import record
from meridian.idempotency import remember, replay
from meridian.identity import DB, Actor, CurrentActor
from meridian.ingestion_models import SnapshotPublication
from meridian.models import WorkerHeartbeat, now
from meridian.people_models import Snapshot
from meridian.scenario_models import Scenario, ScenarioRevision, ScenarioVerification
from meridian.scenario_schemas import ReasonRequest

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])
Key = Annotated[UUID, Header(alias="Idempotency-Key")]


def selected_scenarios_allowed(db: Session, current: Actor, revisions: list[str]) -> bool:
    if not revisions:
        return True
    allowed = list(
        db.scalars(
            select(ScenarioRevision.id)
            .join(Scenario, ScenarioRevision.scenario_id == Scenario.id)
            .where(
                ScenarioRevision.id.in_(revisions),
                or_(
                    Scenario.owner_id == current.principal.id,
                    Scenario.reviewer_id == current.principal.id,
                ),
            )
        )
    )
    return set(allowed) == set(revisions)


def run_for(db: Session, current: Actor, request: Request, run_id: str) -> AnalyticsRun:
    authorize(current, "analytics.full", db, request)
    run = db.get(AnalyticsRun, run_id)
    if run is None or not selected_scenarios_allowed(
        db, current, [row["revision_id"] for row in run.inputs["scenarios"]]
    ):
        record(
            db,
            current.principal.id,
            "security.analytics_object_denied",
            "analytics_run",
            run_id,
            request.state.request_id,
            False,
            emit=False,
        )
        db.commit()
        raise DomainError(
            404,
            "analytics_run_unavailable",
            "The analytical run is unavailable within your current object scope.",
        )
    return run


@router.get("/options", response_model=AnalyticsOptions)
def options(
    current: CurrentActor,
    db: DB,
    request: Request,
    source_search: Annotated[str | None, Query(max_length=100)] = None,
    snapshot_id: UUID | None = None,
) -> AnalyticsOptions:
    authorize(current, "analytics.manage", db, request)
    sources = (
        select(Snapshot)
        .join(SnapshotPublication, Snapshot.id == SnapshotPublication.snapshot_id)
        .where(Snapshot.status == "PUBLISHED")
    )
    if source_search:
        sources = sources.where(
            Snapshot.title.ilike(
                "%" + source_search.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"
            )
        )
    cycles = select(CompCycle).join(CompExportArtifact, CompExportArtifact.cycle_id == CompCycle.id)
    revisions = (
        select(ScenarioRevision, Scenario)
        .join(Scenario, ScenarioRevision.scenario_id == Scenario.id)
        .join(ScenarioVerification, ScenarioVerification.revision_id == ScenarioRevision.id)
        .where(
            or_(
                Scenario.owner_id == current.principal.id,
                Scenario.reviewer_id == current.principal.id,
            )
        )
    )
    if snapshot_id:
        cycles = cycles.where(CompCycle.snapshot_id == str(snapshot_id))
        revisions = revisions.where(Scenario.base_snapshot_id == str(snapshot_id))
    return AnalyticsOptions(
        snapshots=[
            AnalyticsChoice(id=row.id, label=row.title, as_of=row.source_watermark)
            for row in db.scalars(sources.order_by(SnapshotPublication.ordinal.desc()).limit(30))
        ],
        cycles=[
            AnalyticsChoice(id=row.id, label=row.title, snapshot_id=row.snapshot_id)
            for row in db.scalars(cycles.order_by(CompCycle.created_at.desc()).limit(50))
        ],
        scenario_revisions=[
            AnalyticsChoice(
                id=row.id,
                label=f"{scenario.title} · revision {row.number}",
                snapshot_id=scenario.base_snapshot_id,
            )
            for row, scenario in db.execute(
                revisions.order_by(ScenarioRevision.created_at.desc()).limit(50)
            )
        ],
    )


@router.get("/runs", response_model=AnalyticsList)
def runs(
    current: CurrentActor,
    db: DB,
    request: Request,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 10,
) -> AnalyticsList:
    authorize(current, "analytics.full", db, request)
    # A run cannot make another owner's otherwise restricted scenario visible.
    query = (
        select(AnalyticsRun)
        .where(
            or_(
                AnalyticsRun.owner_id == current.principal.id,
                AnalyticsRun.inputs["scenarios"] == [],
            )
        )
        .order_by(AnalyticsRun.created_at.desc(), AnalyticsRun.id.desc())
    )
    if cursor:
        anchor = db.scalar(query.where(AnalyticsRun.id == str(cursor)).limit(1))
        if anchor is None:
            raise DomainError(422, "analytics_cursor_invalid", "Reload the first analytical page.")
        query = query.where(
            or_(
                AnalyticsRun.created_at < anchor.created_at,
                (AnalyticsRun.created_at == anchor.created_at) & (AnalyticsRun.id < anchor.id),
            )
        )
    items = list(db.scalars(query.limit(limit + 1)))
    return AnalyticsList(
        items=[view(db, row) for row in items[:limit]],
        next_cursor=items[limit - 1].id if len(items) > limit else None,
    )


@router.post("/runs", response_model=AnalyticsRunView, status_code=201)
@audited_command
def start(
    body: AnalyticsRequest, current: CurrentActor, db: DB, request: Request, key: Key
) -> AnalyticsRunView:
    authorize(current, "analytics.manage", db, request)
    if not selected_scenarios_allowed(
        db, current, [str(value) for value in body.scenario_revision_ids]
    ):
        record(
            db,
            current.principal.id,
            "security.analytics_scenario_denied",
            "route",
            request.url.path,
            request.state.request_id,
            False,
            emit=False,
        )
        db.commit()
        raise DomainError(
            404,
            "analytics_scenario_unavailable",
            "A selected scenario revision is unavailable in your current object scope.",
        )
    payload = body.model_dump(mode="json")
    route = "analytics.start"
    prior = replay(db, current.principal.id, route, str(key), payload)
    if prior:
        return view(db, run_for(db, current, request, prior["id"]))
    run = create_run(db, body, actor_id=current.principal.id, request_id=request.state.request_id)
    remember(
        db, current.principal.id, route, str(key), payload, {"id": run.id}, request.state.request_id
    )
    result = view(db, run)
    db.commit()
    return result


@router.get("/runs/{run_id}", response_model=AnalyticsRunView)
def get_run(run_id: UUID, current: CurrentActor, db: DB, request: Request) -> AnalyticsRunView:
    return view(db, run_for(db, current, request, str(run_id)))


def derive(
    db: Session,
    current: Actor,
    request: Request,
    run_id: str,
    reason: str,
    key: str,
    event_id: str | None,
) -> AnalyticsRunView:
    authorize(current, "analytics.manage", db, request)
    parent = run_for(db, current, request, run_id)
    result, _document = verified_result(db, parent)
    if event_id and result.status != "PASS":
        raise DomainError(
            409,
            "analytics_drill_unavailable",
            "Run a delivery drill from a fully reconciled result.",
        )
    payload: dict[str, Any] = {
        "parent_run_id": run_id,
        "reason": reason,
        "dropped_event_id": event_id,
    }
    route = "analytics.gap" if event_id else "analytics.repair"
    prior = replay(db, current.principal.id, route, key, payload)
    if prior:
        return view(db, run_for(db, current, request, prior["id"]))
    run = create_run(
        db,
        derived_request(parent, reason),
        actor_id=current.principal.id,
        request_id=request.state.request_id,
        kind="GAP_DRILL" if event_id else "REPAIR",
        parent=parent,
        dropped_event_id=event_id,
    )
    remember(
        db, current.principal.id, route, key, payload, {"id": run.id}, request.state.request_id
    )
    result_view = view(db, run)
    db.commit()
    return result_view


@router.post("/runs/{run_id}/gap-drill", response_model=AnalyticsRunView, status_code=201)
@audited_command
def gap(
    run_id: UUID,
    body: AnalyticsGapRequest,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
) -> AnalyticsRunView:
    return derive(db, current, request, str(run_id), body.reason, str(key), str(body.event_id))


@router.post("/runs/{run_id}/repair", response_model=AnalyticsRunView, status_code=201)
@audited_command
def repair(
    run_id: UUID, body: ReasonRequest, current: CurrentActor, db: DB, request: Request, key: Key
) -> AnalyticsRunView:
    return derive(db, current, request, str(run_id), body.reason, str(key), None)


@router.post("/runs/{run_id}/retry", response_model=AnalyticsRunView)
@audited_command
def retry(
    run_id: UUID,
    body: ReasonRequest,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    attempts: Annotated[int, Header(alias="If-Match", ge=0)],
) -> AnalyticsRunView:
    authorize(current, "analytics.manage", db, request)
    run = run_for(db, current, request, str(run_id))
    payload = {"run_id": run.id, "expected_attempts": attempts, "reason": body.reason}
    prior = replay(db, current.principal.id, "analytics.retry", str(key), payload)
    if not prior:
        retry_run(
            db,
            run,
            expected_attempts=attempts,
            actor_id=current.principal.id,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        remember(
            db,
            current.principal.id,
            "analytics.retry",
            str(key),
            payload,
            {"id": run.id},
            request.state.request_id,
        )
    result_view = view(db, run)
    if not prior:
        db.commit()
    return result_view


@router.get("/runs/{run_id}/manifest")
def manifest(run_id: UUID, current: CurrentActor, db: DB, request: Request) -> Response:
    run = run_for(db, current, request, str(run_id))
    _result, document = verified_result(db, run)
    return Response(
        content=canonical_bytes(document),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="meridian-analytics-{run.id}.json"'},
    )


@router.get("/evidence", response_model=list[AnalyticsEvidence])
def audit_evidence(current: CurrentActor, db: DB, request: Request) -> list[AnalyticsEvidence]:
    authorize(current, "analytics.evidence", db, request)
    return [
        evidence(db, row)
        for row in db.scalars(
            select(AnalyticsRun).order_by(AnalyticsRun.created_at.desc()).limit(20)
        )
    ]


@router.get("/public", response_model=AnalyticsPublic)
def public(current: CurrentActor, db: DB, request: Request) -> AnalyticsPublic:
    authorize(current, "analytics.read", db, request)
    publication = db.scalar(
        select(AnalyticsPublication).order_by(AnalyticsPublication.created_at.desc()).limit(1)
    )
    if publication is None:
        return AnalyticsPublic(
            pipeline_status="EMPTY",
            data_as_of=None,
            source_watermark=None,
            last_successful_sync=None,
            headcount=None,
            fte=None,
            model_version=None,
        )
    run = db.get(AnalyticsRun, publication.run_id)
    if run is None:
        raise DomainError(
            503, "analytics_source_missing", "The last analytical publication is unavailable."
        )
    result, _document = verified_result(db, run)
    snapshot = db.get(Snapshot, run.snapshot_id)
    if snapshot is None:
        raise DomainError(
            503, "analytics_source_missing", "The pinned analytical source is unavailable."
        )
    latest = db.scalar(select(AnalyticsRun.id).order_by(AnalyticsRun.created_at.desc()).limit(1))
    heartbeat = db.get(WorkerHeartbeat, "analytics")
    offline = heartbeat is None or (now() - heartbeat.last_seen_at).total_seconds() > 60
    suppressed = result.report["headcount"] < 5
    return AnalyticsPublic(
        pipeline_status="OUTAGE"
        if offline
        else "STALE"
        if newest_source(db) != run.snapshot_id or latest != run.id
        else "CURRENT",
        data_as_of=run.as_of,
        source_watermark=snapshot.source_watermark,
        last_successful_sync=result.completed_at,
        headcount=None if suppressed else result.report["headcount"],
        fte=None if suppressed else result.report["fte"],
        model_version=run.model_version,
        suppressed=suppressed,
    )
