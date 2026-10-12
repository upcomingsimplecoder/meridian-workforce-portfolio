"""Human-authored review packets with frozen ownership and append-only evidence."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now, uid


class TalentPolicy(Base):
    __tablename__ = "talent_policy_version"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TalentCycle(Base):
    __tablename__ = "talent_cycle"
    __table_args__ = (
        CheckConstraint("state IN ('CONFIGURING','ACTIVE','LOCKED')", name="talent_cycle_state"),
        CheckConstraint("version >= 1 AND lock_number >= 0", name="talent_cycle_versions"),
        CheckConstraint("due_date >= as_of", name="talent_cycle_deadline"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str] = mapped_column(ForeignKey("talent_policy_version.id"))
    authz_policy_id: Mapped[str] = mapped_column(ForeignKey("authz_policy_version.id"))
    authz_sha256: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    assignments: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    scope_orgs: Mapped[list[str]] = mapped_column(JSONB)
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(12), default="CONFIGURING")
    version: Mapped[int] = mapped_column(Integer, default=1)
    lock_number: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TalentCycleAction(Base):
    __tablename__ = "talent_cycle_action"
    __table_args__ = (
        UniqueConstraint("cycle_id", "expected_version"),
        CheckConstraint(
            "action IN ('CREATE','LAUNCH','LOCK','REOPEN')", name="talent_cycle_action"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("talent_cycle.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    expected_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(String(240))
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewPacket(Base):
    __tablename__ = "review_packet"
    __table_args__ = (
        UniqueConstraint("cycle_id", "employee_id"),
        UniqueConstraint("id", "cycle_id"),
        CheckConstraint(
            "author_id <> calibrator_id AND author_id <> hr_signer_id AND calibrator_id <> hr_signer_id",
            name="talent_separation_of_duties",
        ),
        CheckConstraint(
            "state IN ('BUILDING','DRAFT','SUBMITTED','CALIBRATING','CALIBRATED','RETURNED','APPROVED','LOCKED')",
            name="talent_packet_state",
        ),
        CheckConstraint(
            "version >= 0 AND current_revision BETWEEN 0 AND 101",
            name="talent_packet_versions",
        ),
        ForeignKeyConstraint(
            ["id", "current_revision"],
            ["packet_revision.packet_id", "packet_revision.number"],
            use_alter=True,
            name="talent_packet_head",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("talent_cycle.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    calibrator_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    hr_signer_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("employee_version.id"))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    context_sha256: Mapped[str] = mapped_column(String(64))
    chain_ids: Mapped[list[str]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(14), default="BUILDING")
    version: Mapped[int] = mapped_column(Integer, default=0)
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PacketRevision(Base):
    __tablename__ = "packet_revision"
    __table_args__ = (
        UniqueConstraint("packet_id", "number"),
        UniqueConstraint("id", "packet_id"),
        CheckConstraint("number BETWEEN 1 AND 101", name="talent_revision_number"),
        CheckConstraint(
            "kind IN ('INITIAL','EDIT','CALIBRATE','REOPEN')", name="talent_revision_kind"
        ),
        CheckConstraint("jsonb_typeof(review) = 'object'", name="talent_review_object"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    packet_id: Mapped[str] = mapped_column(ForeignKey("review_packet.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("packet_revision.id"))
    parent_sha256: Mapped[str | None] = mapped_column(String(64))
    review: Mapped[dict[str, Any]] = mapped_column(JSONB)
    author_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    reason: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TalentDecision(Base):
    __tablename__ = "talent_decision"
    __table_args__ = (
        UniqueConstraint("packet_id", "expected_version"),
        ForeignKeyConstraint(
            ["revision_id", "packet_id"], ["packet_revision.id", "packet_revision.packet_id"]
        ),
        CheckConstraint(
            "action IN ('INITIAL','EDIT','SUBMIT','START_CALIBRATION','CALIBRATE','ACK_CALIBRATION','RETURN','APPROVE','COMMENT','LOCK','REOPEN')",
            name="talent_decision_action",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    packet_id: Mapped[str] = mapped_column(ForeignKey("review_packet.id"), index=True)
    revision_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    action: Mapped[str] = mapped_column(String(24))
    expected_version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(240))
    acknowledgement: Mapped[bool] = mapped_column(Boolean, default=False)
    authorization: Mapped[dict[str, Any]] = mapped_column(JSONB)
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CalibrationEvent(Base):
    __tablename__ = "calibration_event"
    revision_id: Mapped[str] = mapped_column(ForeignKey("packet_revision.id"), primary_key=True)
    before_revision_id: Mapped[str] = mapped_column(ForeignKey("packet_revision.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    reason: Mapped[str] = mapped_column(String(240))
    before_sha256: Mapped[str] = mapped_column(String(64))
    after_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TalentComment(Base):
    __tablename__ = "talent_comment"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    packet_id: Mapped[str] = mapped_column(ForeignKey("review_packet.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("packet_revision.id"))
    decision_id: Mapped[str] = mapped_column(ForeignKey("talent_decision.id"), unique=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    body: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TalentLock(Base):
    __tablename__ = "talent_lock"
    __table_args__ = (UniqueConstraint("cycle_id", "number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("talent_cycle.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    cycle_action_id: Mapped[str] = mapped_column(ForeignKey("talent_cycle_action.id"), unique=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TalentExport(Base):
    __tablename__ = "talent_export"
    lock_id: Mapped[str] = mapped_column(ForeignKey("talent_lock.id"), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
