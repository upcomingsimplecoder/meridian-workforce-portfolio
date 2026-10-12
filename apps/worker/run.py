import argparse
import logging
import time
from contextlib import ExitStack
from datetime import timedelta

from meridian.compensation_exports import mark_failed_export, write_export
from meridian.compensation_service import verify_revision as verify_comp_revision
from meridian.db import SessionLocal, worker_session_factory
from meridian.ingestion_service import validate_batch, write_manifest
from meridian.models import ConsumerReceipt, OutboxEvent, now
from meridian.replay_models import ReplayAttempt, ReplayCapsule
from meridian.scenario_service import verify_revision, write_approved_manifest
from meridian.schemas import (
    CompCycleEventPayload,
    CompProposalEventPayload,
    EventEnvelope,
    ReplayEventPayload,
    ScenarioEventPayload,
    TalentCycleEventPayload,
)
from meridian.talent_exports import write_export as write_talent_export
from meridian.telemetry import consumer_span
from meridian.worker_liveness import update_heartbeat
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError
from sqlalchemy import select

logger = logging.getLogger("meridian.worker")


def tick() -> int:
    with SessionLocal.begin() as liveness:
        update_heartbeat(liveness, "outbox")
    with ExitStack() as spans, SessionLocal.begin() as db:
        event = db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.state == "PENDING",
                OutboxEvent.next_attempt_at <= now(),
                ~OutboxEvent.event_type.like("analytics.%"),
            )
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return 0
        span = spans.enter_context(consumer_span("meridian-worker", event, db, SessionLocal))
        consumer = (
            "ingestion-v1"
            if event.event_type.startswith("source_batch.")
            else "planning-v1"
            if event.event_type.startswith("scenario.")
            else "compensation-v1"
            if event.event_type.startswith("comp.")
            else "talent-v1"
            if event.event_type.startswith("talent.")
            else "replay-v1"
            if event.event_type.startswith("governance.")
            else "foundation"
        )
        receipt = db.get(ConsumerReceipt, (consumer, event.id))
        if receipt is None:
            try:
                with db.begin_nested():
                    envelope = EventEnvelope.model_validate(
                        {
                            "event_id": event.id,
                            "event_type": event.event_type,
                            "schema_version": event.schema_version,
                            "aggregate_id": event.aggregate_id,
                            "aggregate_version": event.aggregate_version,
                            "source_batch_id": event.aggregate_id
                            if consumer == "ingestion-v1"
                            else event.payload.get("source_batch_id")
                            if consumer in {"planning-v1", "compensation-v1", "talent-v1"}
                            else None,
                            "occurred_at": event.occurred_at,
                            "correlation_id": event.request_id,
                            "payload": event.payload,
                        }
                    )
                    if event.event_type == "source_batch.received.v1":
                        validate_batch(db, event.aggregate_id)
                    elif event.event_type == "source_batch.published.v1":
                        write_manifest(db, event.aggregate_id)
                    elif isinstance(envelope.payload, ScenarioEventPayload):
                        handler = (
                            write_approved_manifest
                            if event.event_type == "scenario.approved.v1"
                            else verify_revision
                        )
                        handler(
                            db,
                            event.aggregate_id,
                            str(envelope.payload.revision_id),
                            str(envelope.payload.source_batch_id),
                        )
                    elif isinstance(envelope.payload, CompProposalEventPayload):
                        verify_comp_revision(
                            db,
                            event.aggregate_id,
                            str(envelope.payload.revision_id),
                            str(envelope.payload.cycle_id),
                            str(envelope.payload.source_batch_id),
                        )
                    elif isinstance(envelope.payload, CompCycleEventPayload):
                        write_export(
                            db,
                            event.aggregate_id,
                            str(envelope.payload.lock_id),
                            str(envelope.payload.source_batch_id),
                        )
                    elif isinstance(envelope.payload, TalentCycleEventPayload):
                        write_talent_export(
                            db,
                            event.aggregate_id,
                            str(envelope.payload.lock_id),
                            str(envelope.payload.source_batch_id),
                            aggregate_version=envelope.aggregate_version,
                        )
                    elif isinstance(envelope.payload, ReplayEventPayload):
                        capsule = db.get(ReplayCapsule, str(envelope.payload.entity_id))
                        attempt = db.get(ReplayAttempt, str(envelope.payload.attempt_id))
                        if (
                            capsule is None
                            or attempt is None
                            or attempt.capsule_id != capsule.id
                            or attempt.number != envelope.aggregate_version
                            or capsule.sha256 != envelope.payload.capsule_sha256
                        ):
                            raise ValueError("REPLAY_EVENT_BINDING_MISMATCH")
            except Exception as error:
                span.set_status(Status(StatusCode.ERROR))
                event.attempts += 1
                event.last_error = (
                    "UNSUPPORTED_EVENT_SCHEMA"
                    if isinstance(error, ValidationError)
                    else "JOB_EXECUTION_FAILED"
                )
                event.state = "DEAD" if event.attempts >= 5 else "PENDING"
                event.next_attempt_at = now() + timedelta(seconds=2**event.attempts)
                if (
                    event.state == "DEAD"
                    and event.event_type == "comp.cycle_locked.v1"
                    and not isinstance(error, ValidationError)
                ):
                    mark_failed_export(
                        db,
                        event.aggregate_id,
                        str(event.payload.get("lock_id", "")),
                        str(event.payload.get("source_batch_id", "")),
                        event.request_id,
                    )
                return 0
            db.add(ConsumerReceipt(consumer=consumer, event_id=event.id))
        event.state = "DONE"
        return 1


def main() -> None:
    global SessionLocal
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    SessionLocal = worker_session_factory()
    while True:
        processed = 0
        try:
            processed = tick()
        except Exception:
            logger.error("worker_iteration_failed")
            if args.once:
                raise
        if args.once:
            return
        # Drain ready work immediately; the poll interval only applies to idle
        # or failed iterations. Retry timestamps still enforce job backoff.
        if not processed:
            time.sleep(2)


if __name__ == "__main__":
    main()
