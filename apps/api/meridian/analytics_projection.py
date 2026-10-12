"""Actual persisted freshness with explicit historical, drift and outage states."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.analytics_models import (
    AnalyticsArtifact,
    AnalyticsPublication,
    AnalyticsResult,
    AnalyticsRun,
)
from meridian.analytics_pipeline import verified_result
from meridian.analytics_schemas import (
    AnalyticsComparison,
    AnalyticsEvidence,
    AnalyticsManifestArtifact,
    AnalyticsMoney,
    AnalyticsRunView,
    AnalyticsScenario,
)
from meridian.analytics_service import event_for
from meridian.ingestion_models import SnapshotPublication
from meridian.models import WorkerHeartbeat, now
from meridian.people_models import Snapshot


def newest_source(db: Session) -> str | None:
    return db.scalar(
        select(SnapshotPublication.snapshot_id)
        .order_by(SnapshotPublication.ordinal.desc())
        .limit(1)
    )


def view(db: Session, run: AnalyticsRun) -> AnalyticsRunView:
    snapshot = db.get(Snapshot, run.snapshot_id)
    if snapshot is None:
        raise RuntimeError("Pinned analytical source is missing")
    event = event_for(db, run)
    heartbeat = db.get(WorkerHeartbeat, "analytics")
    result = db.get(AnalyticsResult, run.id)
    report = verified_result(db, run)[0].report if result else {}
    publication = db.scalar(
        select(AnalyticsPublication)
        .where(AnalyticsPublication.snapshot_id == run.snapshot_id)
        .order_by(AnalyticsPublication.created_at.desc(), AnalyticsPublication.id.desc())
        .limit(1)
    )
    last_good = db.get(AnalyticsResult, publication.run_id) if publication else None
    latest_attempt = db.scalar(
        select(AnalyticsRun.id)
        .where(AnalyticsRun.snapshot_id == run.snapshot_id)
        .order_by(AnalyticsRun.created_at.desc(), AnalyticsRun.id.desc())
        .limit(1)
    )
    historical = newest_source(db) != run.snapshot_id or latest_attempt != run.id
    status = result.status if result else "FAILED" if event.state == "DEAD" else "QUEUED"
    offline = heartbeat is None or (now() - heartbeat.last_seen_at).total_seconds() > 60
    pipeline = (
        "DRIFT"
        if status == "DRIFT"
        else "OUTAGE"
        if status == "FAILED" or offline
        else "PENDING"
        if status == "QUEUED"
        else "HISTORICAL"
        if historical
        else "CURRENT"
    )
    return AnalyticsRunView.model_validate(
        {
            "id": run.id,
            "snapshot_id": run.snapshot_id,
            "snapshot_title": snapshot.title,
            "source_batch_id": snapshot.source_batch_id,
            "source_watermark": snapshot.source_watermark,
            "data_as_of": run.as_of,
            "source_captured_at": snapshot.captured_at,
            "created_at": run.created_at,
            "last_successful_sync": last_good.completed_at if last_good else None,
            "kind": run.kind,
            "status": status,
            "pipeline_status": pipeline,
            "model_version": run.model_version,
            "model_sha256": run.model_sha256,
            "inputs_sha256": run.inputs_sha256,
            "result_sha256": result.sha256 if result else None,
            "parent_run_id": run.parent_run_id,
            "dropped_event_id": run.dropped_event_id,
            "attempts": event.attempts,
            "job_error": event.last_error,
            "worker_last_seen": heartbeat.last_seen_at if heartbeat else None,
            "headcount": report.get("headcount"),
            "fte": report.get("fte"),
            "employee_versions": report.get("employee_versions"),
            "compensation": [
                AnalyticsMoney.model_validate(row) for row in report.get("compensation", [])
            ],
            "scenarios": [
                AnalyticsScenario.model_validate(row) for row in report.get("scenarios", [])
            ],
            "comparisons": [
                AnalyticsComparison.model_validate(row) for row in report.get("comparisons", [])
            ],
            "artifacts": [
                AnalyticsManifestArtifact(
                    name=row.name,
                    sha256=row.storage_key,
                    rows_sha256=row.rows_sha256,
                    row_count=row.row_count,
                    schema_version=row.schema_version,
                )
                for row in db.scalars(
                    select(AnalyticsArtifact)
                    .where(AnalyticsArtifact.run_id == run.id)
                    .order_by(AnalyticsArtifact.name)
                )
            ],
            "delivery_event_ids": [
                row["event_id"]
                for row in run.inputs["cycles"]
                if row["event_id"] != run.dropped_event_id and result is not None
            ],
            "historical": historical,
        }
    )


def evidence(db: Session, run: AnalyticsRun) -> AnalyticsEvidence:
    result = db.get(AnalyticsResult, run.id)
    if result:
        result, _document = verified_result(db, run)
    event = event_for(db, run)
    comparisons = result.report["comparisons"] if result else []
    return AnalyticsEvidence(
        id=run.id,
        snapshot_id=run.snapshot_id,
        data_as_of=run.as_of,
        model_version=run.model_version,
        inputs_sha256=run.inputs_sha256,
        result_sha256=result.sha256 if result else None,
        status=result.status if result else "FAILED" if event.state == "DEAD" else "QUEUED",
        last_successful_sync=result.completed_at if result and result.status == "PASS" else None,
        comparison_count=len(comparisons),
        failed_comparisons=sum(not row["matches"] for row in comparisons),
        source_versions=result.report["employee_versions"] if result else None,
    )
