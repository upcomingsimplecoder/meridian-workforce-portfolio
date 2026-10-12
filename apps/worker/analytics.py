"""Independently runnable analytics outbox consumer; domain approvals stay available."""

import argparse
import logging
import subprocess
import time
from contextlib import ExitStack
from datetime import timedelta

from meridian.analytics_models import AnalyticsRun
from meridian.analytics_pipeline import execute_run
from meridian.db import SessionLocal, worker_session_factory
from meridian.models import ConsumerReceipt, OutboxEvent, now
from meridian.schemas import AnalyticsEventPayload, EventEnvelope
from meridian.telemetry import consumer_span
from meridian.worker_liveness import update_heartbeat
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError
from sqlalchemy import select

logger = logging.getLogger("meridian.analytics_worker")


def tick() -> int:
    with SessionLocal.begin() as liveness:
        update_heartbeat(liveness, "analytics")
    with ExitStack() as spans, SessionLocal.begin() as db:
        event = db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.state == "PENDING",
                OutboxEvent.event_type.like("analytics.%"),
                OutboxEvent.next_attempt_at <= now(),
            )
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return 0
        span = spans.enter_context(
            consumer_span("meridian-analytics-worker", event, db, SessionLocal)
        )
        if db.get(ConsumerReceipt, ("analytics-v1", event.id)) is None:
            try:
                with db.begin_nested():
                    envelope = EventEnvelope.model_validate(
                        {
                            "event_id": event.id,
                            "event_type": event.event_type,
                            "schema_version": event.schema_version,
                            "aggregate_id": event.aggregate_id,
                            "aggregate_version": event.aggregate_version,
                            "source_batch_id": event.payload.get("source_batch_id"),
                            "occurred_at": event.occurred_at,
                            "correlation_id": event.request_id,
                            "payload": event.payload,
                        }
                    )
                    run = db.get(AnalyticsRun, event.aggregate_id)
                    if (
                        not isinstance(envelope.payload, AnalyticsEventPayload)
                        or run is None
                        or envelope.aggregate_version != 1
                        or envelope.payload.inputs_sha256 != run.inputs_sha256
                        or str(envelope.source_batch_id) != run.inputs["source_batch_id"]
                        or str(envelope.correlation_id) != run.request_id
                    ):
                        raise ValueError("Analytical event does not match its frozen request")
                    execute_run(db, run, preserve=(event,))
            except Exception as error:
                span.set_status(Status(StatusCode.ERROR))
                event.attempts += 1
                event.state = "DEAD" if event.attempts >= 5 else "PENDING"
                event.last_error = (
                    "UNSUPPORTED_EVENT_SCHEMA"
                    if isinstance(error, ValidationError)
                    else "ANALYTICS_EXECUTION_TIMEOUT"
                    if isinstance(error, subprocess.TimeoutExpired)
                    else "ANALYTICS_EXECUTION_FAILED"
                )
                event.next_attempt_at = now() + timedelta(seconds=2**event.attempts)
                return 0
            db.add(ConsumerReceipt(consumer="analytics-v1", event_id=event.id))
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
            logger.error("analytics_worker_iteration_failed")
            if args.once:
                raise
        if args.once:
            return
        if not processed:
            time.sleep(2)


if __name__ == "__main__":
    main()
