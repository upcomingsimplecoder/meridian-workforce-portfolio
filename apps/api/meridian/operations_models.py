"""Technical observability metadata contains no personnel or command payloads."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now


class OutboxTrace(Base):
    __tablename__ = "outbox_trace"
    __table_args__ = (
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND parent_span_id ~ '^[0-9a-f]{16}$'",
            name="outbox_trace_ids",
        ),
    )
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_event.id"), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    parent_span_id: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TraceSpan(Base):
    __tablename__ = "trace_span"
    __table_args__ = (
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND span_id ~ '^[0-9a-f]{16}$'", name="trace_span_ids"
        ),
        CheckConstraint(
            "service IN ('meridian-api','meridian-worker','meridian-analytics-worker')",
            name="trace_span_service",
        ),
        CheckConstraint(
            "kind IN ('SERVER','CONSUMER') AND outcome IN ('OK','ERROR')",
            name="trace_span_kind_outcome",
        ),
        CheckConstraint(
            "duration_ms >= 0 AND (http_status IS NULL OR http_status BETWEEN 100 AND 599)",
            name="trace_span_bounds",
        ),
    )
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    service: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(10))
    outcome: Mapped[str] = mapped_column(String(5))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    http_status: Mapped[int | None]
    route: Mapped[str | None] = mapped_column(String(200))
    event_type: Mapped[str | None] = mapped_column(String(100))
    event_id: Mapped[str | None] = mapped_column(String(36), index=True)
