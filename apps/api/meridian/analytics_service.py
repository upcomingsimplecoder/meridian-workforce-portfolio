"""Frozen analytical requests and transactional, idempotent delivery intents."""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.analytics_exports import MODEL_VERSION, extract, model_fingerprint
from meridian.analytics_models import AnalyticsResult, AnalyticsRun
from meridian.analytics_schemas import AnalyticsRequest
from meridian.errors import DomainError
from meridian.governance import record
from meridian.models import OutboxEvent
from meridian.policy import POLICY_ID, canonical_hash


def create_run(
    db: Session,
    body: AnalyticsRequest,
    *,
    actor_id: str,
    request_id: str,
    kind: Literal["SYNC", "GAP_DRILL", "REPAIR"] = "SYNC",
    parent: AnalyticsRun | None = None,
    dropped_event_id: str | None = None,
) -> AnalyticsRun:
    if not body.reason.strip() or not 1900 <= body.as_of.year <= 2199:
        raise DomainError(
            422,
            "analytics_input_invalid",
            "A valid calendar date and meaningful analytical reason are required.",
        )
    _data, inputs = extract(
        db,
        str(body.snapshot_id),
        body.as_of,
        [str(value) for value in body.cycle_ids],
        [str(value) for value in body.scenario_revision_ids],
    )
    fingerprint = model_fingerprint()
    if parent and (
        inputs != parent.inputs
        or fingerprint != parent.model_sha256
        or parent.model_version != MODEL_VERSION
    ):
        raise DomainError(
            409,
            "analytics_pins_changed",
            "A repair or delivery drill must preserve the exact original inputs and model. Create an explicit new sync for changed versions.",
        )
    if dropped_event_id and (
        kind != "GAP_DRILL" or dropped_event_id not in {row["event_id"] for row in inputs["cycles"]}
    ):
        raise DomainError(
            404,
            "analytics_delivery_unavailable",
            "This exact delivery event is not part of the selected analytical input.",
        )
    run = AnalyticsRun(
        owner_id=actor_id,
        snapshot_id=str(body.snapshot_id),
        as_of=body.as_of,
        authz_policy_id=POLICY_ID,
        model_version=MODEL_VERSION,
        model_sha256=fingerprint,
        inputs=inputs,
        inputs_sha256=canonical_hash(inputs),
        kind=kind,
        parent_run_id=parent.id if parent else None,
        dropped_event_id=dropped_event_id,
        reason=body.reason,
        request_id=request_id,
    )
    db.add(run)
    db.flush()
    record(
        db,
        actor_id,
        "analytics.run_requested",
        "analytics_run",
        run.id,
        request_id,
        after_hash=run.inputs_sha256,
        event_payload={
            "entity_type": "analytics_run",
            "entity_id": run.id,
            "source_batch_id": inputs["source_batch_id"],
            "inputs_sha256": run.inputs_sha256,
        },
    )
    db.flush()
    return run


def derived_request(parent: AnalyticsRun, reason: str) -> AnalyticsRequest:
    return AnalyticsRequest.model_validate(
        {
            "snapshot_id": parent.snapshot_id,
            "as_of": parent.as_of,
            "cycle_ids": [row["cycle_id"] for row in parent.inputs["cycles"]],
            "scenario_revision_ids": [row["revision_id"] for row in parent.inputs["scenarios"]],
            "reason": reason,
        }
    )


def event_for(db: Session, run: AnalyticsRun) -> OutboxEvent:
    event = db.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.event_type == "analytics.run_requested.v1",
            OutboxEvent.aggregate_id == run.id,
        )
        .order_by(OutboxEvent.occurred_at)
        .limit(1)
    )
    if event is None:
        raise DomainError(
            503, "analytics_event_missing", "The durable analytical delivery intent is missing."
        )
    return event


def retry_run(
    db: Session,
    run: AnalyticsRun,
    *,
    expected_attempts: int,
    actor_id: str,
    reason: str,
    request_id: str,
) -> None:
    event = db.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.id == event_for(db, run).id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or event.attempts != expected_attempts:
        raise DomainError(
            409,
            "analytics_job_changed",
            "The analytical job changed; reload its current attempt count.",
        )
    if event.state != "DEAD" or db.get(AnalyticsResult, run.id) is not None:
        raise DomainError(
            409, "analytics_retry_unavailable", "Only an exhausted analytical job can be retried."
        )
    from meridian.models import now

    event.state, event.attempts, event.last_error, event.next_attempt_at = "PENDING", 0, None, now()
    record(
        db,
        actor_id,
        "analytics.job_retried",
        "analytics_run",
        run.id,
        request_id,
        after_hash=canonical_hash(
            {"reason": reason, "event_id": event.id, "inputs_sha256": run.inputs_sha256}
        ),
        emit=False,
    )
