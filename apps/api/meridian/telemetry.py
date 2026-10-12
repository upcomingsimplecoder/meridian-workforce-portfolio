"""Real OTel SDK spans; explicit export projection excludes values and exceptions."""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, get_args
from uuid import UUID

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)
from sqlalchemy.orm import Session

from meridian import db as database
from meridian.models import OutboxEvent
from meridian.operations_models import OutboxTrace, TraceSpan
from meridian.schemas import EventEnvelope

logger = logging.getLogger("meridian.telemetry")
Factory = Callable[[], Session]
_factories: dict[str, Factory] = {}
_providers: dict[str, TracerProvider] = {}
SERVICES = {"meridian-api", "meridian-worker", "meridian-analytics-worker"}
EVENT_TYPES = set(get_args(EventEnvelope.model_fields["event_type"].annotation))


def opaque_uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        return None


def safe_route(value: Any) -> str:
    from meridian.main import app

    # FastAPI includes router groups lazily. OpenAPI flattens the registered
    # routes and returns their parameter templates without request values.
    routes = set(app.openapi()["paths"]) | {
        "/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
    }
    return (
        value if isinstance(value, str) and value in routes and len(value) <= 200 else "[unmatched]"
    )


class DatabaseExporter(SpanExporter):
    def __init__(self, service: str):
        self.service = service

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with _factories[self.service]() as db, db.begin():
                for span in spans:
                    context = span.context
                    attributes = span.attributes or {}
                    request_id = opaque_uuid(attributes.get("meridian.request_id"))
                    if (
                        context is None
                        or span.start_time is None
                        or span.end_time is None
                        or request_id is None
                    ):
                        continue
                    if span.kind not in {SpanKind.SERVER, SpanKind.CONSUMER}:
                        continue
                    http_status = attributes.get("http.response.status_code")
                    event_type = attributes.get("messaging.event_type")
                    db.add(
                        TraceSpan(
                            span_id=f"{context.span_id:016x}",
                            trace_id=f"{context.trace_id:032x}",
                            parent_span_id=f"{span.parent.span_id:016x}" if span.parent else None,
                            request_id=request_id,
                            service=self.service,
                            kind=span.kind.name,
                            outcome="ERROR"
                            if span.status.status_code == StatusCode.ERROR
                            else "OK",
                            started_at=datetime.fromtimestamp(span.start_time / 1_000_000_000, UTC),
                            duration_ms=Decimal(span.end_time - span.start_time)
                            / Decimal(1_000_000),
                            http_status=http_status
                            if isinstance(http_status, int) and 100 <= http_status <= 599
                            else None,
                            route=safe_route(attributes.get("http.route"))
                            if span.kind == SpanKind.SERVER
                            else None,
                            event_type=event_type
                            if event_type in EVENT_TYPES
                            else "[unsupported]"
                            if span.kind == SpanKind.CONSUMER
                            else None,
                            event_id=opaque_uuid(attributes.get("messaging.event_id")),
                        )
                    )
            return SpanExportResult.SUCCESS
        except Exception:
            # No driver exception, headers, SQL, connection, narrative or input value is logged.
            logger.error("telemetry_export_unavailable")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None


def tracer(service: str, factory: Factory | None = None) -> trace.Tracer:
    if service not in SERVICES:
        raise ValueError("Unknown telemetry service")
    if factory is not None:
        _factories[service] = factory
    elif service not in _factories:
        _factories[service] = lambda: database.SessionLocal()
    if service not in _providers:
        provider = TracerProvider(
            resource=Resource.create({"service.name": service}), shutdown_on_exit=False
        )
        provider.add_span_processor(SimpleSpanProcessor(DatabaseExporter(service)))
        _providers[service] = provider
    return _providers[service].get_tracer("meridian.explicit", "1")


def pin_event(db: Session, event_id: str) -> None:
    span = trace.get_current_span()
    context = span.get_span_context()
    # Only an originating API request creates the immutable API parent map.
    # Worker-emitted events retain their request ID but do not invent an API
    # parent or require permission to attach metadata to existing human events.
    if context.is_valid and getattr(span, "kind", None) == SpanKind.SERVER:
        db.add(
            OutboxTrace(
                event_id=event_id,
                trace_id=f"{context.trace_id:032x}",
                parent_span_id=f"{context.span_id:016x}",
            )
        )


@contextmanager
def server_span(request_id: str) -> Iterator[trace.Span]:
    # Ignore user traceparent/baggage. Authorization and correlation are server-generated.
    with tracer("meridian-api").start_as_current_span(
        "HTTP request",
        context=Context(),
        kind=SpanKind.SERVER,
        attributes={"meridian.request_id": request_id},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException:
            span.set_status(Status(StatusCode.ERROR))
            raise


def finish_http(route: str, status: int) -> None:
    span = trace.get_current_span()
    span.set_attribute("http.route", safe_route(route))
    span.set_attribute("http.response.status_code", status)
    span.set_status(Status(StatusCode.ERROR if status >= 500 else StatusCode.OK))


@contextmanager
def consumer_span(
    service: str, event: OutboxEvent, db: Session, factory: Factory
) -> Iterator[trace.Span]:
    origin = db.get(OutboxTrace, event.id)
    parent = (
        trace.set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=int(origin.trace_id, 16),
                    span_id=int(origin.parent_span_id, 16),
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            )
        )
        if origin
        else Context()
    )
    with tracer(service, factory).start_as_current_span(
        "outbox.consume",
        context=parent,
        kind=SpanKind.CONSUMER,
        attributes={
            "meridian.request_id": event.request_id,
            "messaging.event_id": event.id,
            "messaging.event_type": event.event_type,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException:
            span.set_status(Status(StatusCode.ERROR))
            raise
