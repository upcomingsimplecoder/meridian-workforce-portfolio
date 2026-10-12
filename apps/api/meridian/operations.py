"""Bounded, authorized technical telemetry; never projects personnel or payloads."""

import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from meridian.analytics_models import AnalyticsPublication
from meridian.compensation_auth import scopes
from meridian.errors import DomainError
from meridian.governance import record
from meridian.identity import DB, CurrentActor
from meridian.ingestion_models import SnapshotPublication
from meridian.models import OutboxEvent, WorkerHeartbeat, now
from meridian.operations_models import TraceSpan

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


class SpanView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    span_id: str
    trace_id: str
    parent_span_id: str | None
    request_id: str
    service: str
    kind: str
    outcome: str
    started_at: datetime
    duration_ms: Decimal
    http_status: int | None
    route: str | None
    event_type: str | None
    event_id: str | None


class WorkerView(BaseModel):
    name: str
    last_seen_at: datetime | None
    age_seconds: int | None
    state: Literal["HEALTHY", "STALE", "UNOBSERVED"]


class AlertView(BaseModel):
    code: str
    detail: str


class OperationsView(BaseModel):
    measured_at: datetime
    window_started_at: datetime
    sample_limit: int
    sample_truncated: bool
    http_requests: int
    http_errors: int
    denied_requests: int
    http_p50_ms: Decimal | None
    http_p95_ms: Decimal | None
    pending_jobs: int
    dead_jobs: int
    oldest_pending_seconds: int | None
    workers: list[WorkerView]
    last_analytics_publication: datetime | None
    analytics_matches_latest_snapshot: bool
    alerts: list[AlertView]
    recent_spans: list[SpanView]


class TraceView(BaseModel):
    trace_id: str
    spans: list[SpanView]
    span_limit: int
    truncated: bool


def authorize(current: CurrentActor, db: DB, request: Request) -> None:
    if "*" not in scopes(current, "operations.read"):
        record(
            db,
            current.principal.id,
            "security.operations_denied",
            "route",
            request.url.path,
            request.state.request_id,
            False,
            emit=False,
        )
        db.commit()
        raise DomainError(
            403, "operations_forbidden", "Your current grants do not permit operational telemetry."
        )


def percentile(values: list[Decimal], percentile: int) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile / 100) - 1)]


@router.get("", response_model=OperationsView)
def dashboard(current: CurrentActor, db: DB, request: Request) -> OperationsView:
    authorize(current, db, request)
    measured = now()
    window = measured - timedelta(minutes=15)
    rows = list(
        db.scalars(
            select(TraceSpan)
            .where(TraceSpan.started_at >= window)
            .order_by(TraceSpan.started_at.desc(), TraceSpan.span_id)
            .limit(5001)
        )
    )
    truncated = len(rows) > 5000
    rows = rows[:5000]
    http = [span for span in rows if span.kind == "SERVER"]
    pending, oldest = db.execute(
        select(func.count(), func.min(OutboxEvent.occurred_at)).where(
            OutboxEvent.state == "PENDING"
        )
    ).one()
    dead = (
        db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.state == "DEAD"))
        or 0
    )
    workers = []
    alerts = []
    for name in ("outbox", "analytics"):
        heartbeat = db.get(WorkerHeartbeat, name)
        age = (
            max(0, int((measured - heartbeat.last_seen_at).total_seconds())) if heartbeat else None
        )
        state: Literal["HEALTHY", "STALE", "UNOBSERVED"] = (
            "UNOBSERVED" if age is None else "STALE" if age > 120 else "HEALTHY"
        )
        workers.append(
            WorkerView(
                name=name,
                last_seen_at=heartbeat.last_seen_at if heartbeat else None,
                age_seconds=age,
                state=state,
            )
        )
        if state != "HEALTHY":
            alerts.append(
                AlertView(
                    code="worker_" + name + "_" + state.lower(),
                    detail=name + " worker has no heartbeat within 120 seconds.",
                )
            )
    lag = max(0, int((measured - oldest).total_seconds())) if oldest else None
    if lag is not None and lag > 60:
        alerts.append(
            AlertView(code="job_lag", detail="Oldest pending event is more than 60 seconds old.")
        )
    if dead:
        alerts.append(
            AlertView(
                code="dead_jobs",
                detail="Failed jobs require investigation and an authorized retry.",
            )
        )
    publication = db.scalar(
        select(AnalyticsPublication).order_by(AnalyticsPublication.created_at.desc()).limit(1)
    )
    latest = db.scalar(
        select(SnapshotPublication).order_by(SnapshotPublication.ordinal.desc()).limit(1)
    )
    matches = (
        publication is not None
        and latest is not None
        and publication.snapshot_id == latest.snapshot_id
    )
    if (
        publication is None
        or not matches
        or measured - publication.created_at > timedelta(minutes=15)
    ):
        alerts.append(
            AlertView(
                code="analytics_freshness",
                detail="Analytics lacks a recent publication for the latest published snapshot.",
            )
        )
    errors = sum(span.http_status is not None and span.http_status >= 500 for span in http)
    if errors:
        alerts.append(
            AlertView(
                code="http_errors",
                detail="One or more server errors occurred in the observed window.",
            )
        )
    return OperationsView(
        measured_at=measured,
        window_started_at=window,
        sample_limit=5000,
        sample_truncated=truncated,
        http_requests=len(http),
        http_errors=errors,
        denied_requests=sum(span.http_status == 403 for span in http),
        http_p50_ms=percentile([span.duration_ms for span in http], 50),
        http_p95_ms=percentile([span.duration_ms for span in http], 95),
        pending_jobs=pending,
        dead_jobs=dead,
        oldest_pending_seconds=lag,
        workers=workers,
        last_analytics_publication=publication.created_at if publication else None,
        analytics_matches_latest_snapshot=matches,
        alerts=alerts,
        recent_spans=[SpanView.model_validate(span) for span in rows[:50]],
    )


@router.get("/traces/{trace_id}", response_model=TraceView)
def detail(trace_id: str, current: CurrentActor, db: DB, request: Request) -> TraceView:
    authorize(current, db, request)
    try:
        parsed = UUID(trace_id)
        if parsed.hex != trace_id:
            raise ValueError
    except ValueError as exc:
        raise DomainError(
            422, "invalid_trace_id", "Trace identifier must be 32 lowercase hexadecimal characters."
        ) from exc
    rows = list(
        db.scalars(
            select(TraceSpan)
            .where(TraceSpan.trace_id == trace_id)
            .order_by(TraceSpan.started_at, TraceSpan.span_id)
            .limit(201)
        )
    )
    if not rows:
        raise DomainError(404, "trace_missing", "The requested trace is unavailable.")
    return TraceView(
        trace_id=trace_id,
        span_limit=200,
        truncated=len(rows) > 200,
        spans=[SpanView.model_validate(row) for row in rows[:200]],
    )
