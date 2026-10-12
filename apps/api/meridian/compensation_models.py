"""Guarded compensation cycles, immutable proposals, serial decisions and artifacts."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now, uid


class CompGuidelineVersion(Base):
    __tablename__ = "comp_guideline_version"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="comp_guideline_sha"),
        CheckConstraint("jsonb_typeof(definition) = 'object'", name="comp_guideline_object"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_label: Mapped[str] = mapped_column(String(100))
    effective_date: Mapped[date] = mapped_column(Date)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompCycle(Base):
    __tablename__ = "comp_cycle"
    __table_args__ = (
        CheckConstraint("version >= 1", name="comp_cycle_positive_version"),
        CheckConstraint(
            "state IN ('CONFIGURING','OPEN','REVIEWING','FINAL_APPROVAL','LOCKED','EXPORTED','RECONCILED','FAILED_EXPORT','CANCELLED')",
            name="comp_cycle_state",
        ),
        ForeignKeyConstraint(
            ["current_round_id", "id"],
            ["comp_review_round.id", "comp_review_round.cycle_id"],
            name="comp_cycle_review_round",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"))
    guideline_id: Mapped[str] = mapped_column(ForeignKey("comp_guideline_version.id"))
    fx_set_id: Mapped[str] = mapped_column(ForeignKey("fx_rate_set.id"))
    authz_policy_id: Mapped[str] = mapped_column(ForeignKey("authz_policy_version.id"))
    terms: Mapped[dict[str, Any]] = mapped_column(JSONB)
    selected_ids: Mapped[list[str]] = mapped_column(JSONB)
    scope_orgs: Mapped[list[str]] = mapped_column(JSONB)
    approvals: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="CONFIGURING", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    current_round_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class CompPool(Base):
    __tablename__ = "comp_pool"
    __table_args__ = (
        UniqueConstraint("cycle_id", "manager_id", "org_id", "currency"),
        UniqueConstraint("id", "cycle_id"),
        CheckConstraint(
            "budget_minor >= 0 AND holdback_minor >= 0 AND holdback_minor <= budget_minor",
            name="comp_pool_budget",
        ),
        CheckConstraint("currency IN ('USD','EUR','GBP','JPY','KWD')", name="comp_pool_currency"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), index=True)
    manager_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("org_unit.id"))
    cost_center: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    budget_minor: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    holdback_minor: Mapped[Decimal] = mapped_column(Numeric(20, 0))


class CompEligibility(Base):
    __tablename__ = "comp_eligibility"
    __table_args__ = (
        ForeignKeyConstraint(["pool_id", "cycle_id"], ["comp_pool.id", "comp_pool.cycle_id"]),
        CheckConstraint(
            "(eligible AND proposer_id IS NOT NULL AND pool_id IS NOT NULL) OR (NOT eligible AND pool_id IS NULL)",
            name="comp_eligibility_pool",
        ),
    )
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    eligible: Mapped[bool]
    proposer_id: Mapped[str | None] = mapped_column(ForeignKey("principal.id"))
    pool_id: Mapped[str | None] = mapped_column(String(36))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))


class CompProposal(Base):
    __tablename__ = "comp_proposal"
    __table_args__ = (
        UniqueConstraint("cycle_id", "employee_id"),
        ForeignKeyConstraint(
            ["cycle_id", "employee_id"],
            ["comp_eligibility.cycle_id", "comp_eligibility.employee_id"],
        ),
        CheckConstraint("version >= 0 AND current_revision >= 0", name="comp_proposal_versions"),
        CheckConstraint(
            "state IN ('BUILDING','DRAFT','SUBMITTED','APPROVAL_PENDING','APPROVED','RETURNED','LOCKED')",
            name="comp_proposal_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id"), index=True)
    proposer_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="BUILDING")
    version: Mapped[int] = mapped_column(Integer, default=0)
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompProposalRevision(Base):
    __tablename__ = "comp_proposal_revision"
    __table_args__ = (
        UniqueConstraint("proposal_id", "number"),
        UniqueConstraint("id", "proposal_id"),
        CheckConstraint("number BETWEEN 1 AND 101", name="comp_revision_bound"),
        CheckConstraint("merit_minor >= 0 AND bonus_minor >= 0", name="comp_revision_money"),
        CheckConstraint("kind IN ('INITIAL','EDIT','RETURN')", name="comp_revision_kind"),
        CheckConstraint("length(btrim(reason)) >= 3", name="comp_revision_reason"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("comp_proposal.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))
    merit_minor: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    bonus_minor: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    comment: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(String(240))
    author_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    award: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompApprovalStep(Base):
    __tablename__ = "comp_approval_step"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "proposal_id"],
            ["comp_proposal_revision.id", "comp_proposal_revision.proposal_id"],
        ),
        CheckConstraint("ordinal BETWEEN 1 AND 3", name="comp_step_ordinal"),
        CheckConstraint("role IN ('DIVISION','HR','FINANCE')", name="comp_step_role"),
    )
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("comp_proposal.id"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    assigned_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    capability: Mapped[str] = mapped_column(String(40))
    exception_codes: Mapped[list[str]] = mapped_column(JSONB)


class CompReviewRound(Base):
    __tablename__ = "comp_review_round"
    __table_args__ = (UniqueConstraint("id", "cycle_id"), UniqueConstraint("cycle_id", "number"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompDecision(Base):
    __tablename__ = "comp_decision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "proposal_id"],
            ["comp_proposal_revision.id", "comp_proposal_revision.proposal_id"],
        ),
        UniqueConstraint("proposal_id", "expected_version"),
        CheckConstraint(
            "action IN ('SUBMIT','START_REVIEW','APPROVE','RETURN','INVALIDATE_FINANCE')",
            name="comp_decision_action",
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="comp_decision_reason"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("comp_proposal.id"), index=True)
    revision_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    round_id: Mapped[str | None] = mapped_column(ForeignKey("comp_review_round.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    expected_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(240))
    acknowledged_exceptions: Mapped[list[str]] = mapped_column(JSONB)
    conflict_check_sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompCycleAction(Base):
    __tablename__ = "comp_cycle_action"
    __table_args__ = (
        UniqueConstraint("cycle_id", "expected_version"),
        CheckConstraint(
            "action IN ('LAUNCH','REVIEWING','OPEN_FINANCE','RETURN_TO_REVIEW','LOCK','EXPORTED','RECONCILE','FAILED_EXPORT','RETRY_EXPORT','CANCEL')",
            name="comp_cycle_action_kind",
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="comp_cycle_action_reason"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), index=True)
    expected_version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("principal.id"))
    action: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(240))
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompRevisionVerification(Base):
    __tablename__ = "comp_revision_verification"
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("comp_proposal_revision.id"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompLock(Base):
    __tablename__ = "comp_lock"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), unique=True)
    round_id: Mapped[str] = mapped_column(ForeignKey("comp_review_round.id"), unique=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle_action.id"), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompExportArtifact(Base):
    __tablename__ = "comp_export_artifact"
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), primary_key=True)
    lock_id: Mapped[str] = mapped_column(ForeignKey("comp_lock.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer)
    csv_key: Mapped[str] = mapped_column(String(64))
    manifest_key: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompReconciliation(Base):
    __tablename__ = "comp_reconciliation"
    cycle_id: Mapped[str] = mapped_column(ForeignKey("comp_cycle.id"), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    export_sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
