"""Immutable provenance capsules, observed reruns and purpose-signed redacted packets."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now, uid


class ReplayCapsule(Base):
    __tablename__ = "replay_capsule"
    __table_args__ = (
        CheckConstraint("kind IN ('SCENARIO','AWARD')", name="replay_capsule_kind"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="replay_capsule_sha"),
        CheckConstraint("length(btrim(reason)) >= 3", name="replay_capsule_reason"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(10))
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(240))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class ReplayAttempt(Base):
    __tablename__ = "replay_attempt"
    __table_args__ = (
        UniqueConstraint("capsule_id", "number"),
        CheckConstraint("number >= 1", name="replay_attempt_number"),
        CheckConstraint(
            "status IN ('MATCH','MISSING','CORRUPT','UNSUPPORTED')", name="replay_attempt_status"
        ),
        CheckConstraint(
            "status <> 'MATCH' OR (observed_sha256 IS NOT NULL AND expected_sha256 = observed_sha256 AND issues = '[]'::jsonb)",
            name="replay_attempt_match",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    capsule_id: Mapped[str] = mapped_column(ForeignKey("replay_capsule.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    status: Mapped[str] = mapped_column(String(12))
    expected_sha256: Mapped[str] = mapped_column(String(64))
    observed_sha256: Mapped[str | None] = mapped_column(String(64))
    issues: Mapped[list[str]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(String(240))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReplayArtifact(Base):
    __tablename__ = "replay_artifact"
    attempt_id: Mapped[str] = mapped_column(ForeignKey("replay_attempt.id"), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
