"""Immutable analytical requests, delivery receipts, results and publications."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now, uid


class AnalyticsRun(Base):
    __tablename__ = "analytics_run"
    __table_args__ = (
        CheckConstraint("kind IN ('SYNC','GAP_DRILL','REPAIR')", name="analytics_run_kind"),
        CheckConstraint("length(btrim(reason)) >= 3", name="analytics_run_reason"),
        CheckConstraint("inputs_sha256 ~ '^[0-9a-f]{64}$'", name="analytics_run_sha"),
        CheckConstraint(
            "(kind = 'GAP_DRILL') = (dropped_event_id IS NOT NULL)", name="analytics_gap_event"
        ),
        CheckConstraint("kind = 'SYNC' OR parent_run_id IS NOT NULL", name="analytics_parent"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    authz_policy_id: Mapped[str] = mapped_column(ForeignKey("authz_policy_version.id"))
    model_version: Mapped[str] = mapped_column(String(50))
    model_sha256: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(12))
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("analytics_run.id"))
    dropped_event_id: Mapped[str | None] = mapped_column(ForeignKey("outbox_event.id"))
    reason: Mapped[str] = mapped_column(String(240))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class AnalyticsArtifact(Base):
    __tablename__ = "analytics_artifact"
    __table_args__ = (CheckConstraint("row_count >= 0", name="analytics_artifact_count"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("analytics_run.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(64))
    rows_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class AnalyticsDelivery(Base):
    __tablename__ = "analytics_delivery"
    run_id: Mapped[str] = mapped_column(ForeignKey("analytics_run.id"), primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AnalyticsResult(Base):
    __tablename__ = "analytics_result"
    __table_args__ = (
        CheckConstraint("status IN ('PASS','DRIFT')", name="analytics_result_status"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("analytics_run.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(8))
    catalog_key: Mapped[str] = mapped_column(String(64))
    manifest_key: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AnalyticsPublication(Base):
    __tablename__ = "analytics_publication"
    __table_args__ = (UniqueConstraint("run_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("analytics_result.run_id"))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
