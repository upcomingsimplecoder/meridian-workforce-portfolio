"""Immutable scenario inputs, revisions, calculations, decisions and plan promotions."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
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


class Scenario(Base):
    __tablename__ = "scenario"
    __table_args__ = (
        CheckConstraint(
            "version >= 0 AND current_revision >= 0", name="scenario_positive_versions"
        ),
        CheckConstraint(
            "state IN ('BUILDING','DRAFT','SUBMITTED','IN_REVIEW','APPROVED','REJECTED','PROMOTED')",
            name="scenario_state",
        ),
        CheckConstraint("owner_id <> reviewer_id", name="scenario_separation_of_duties"),
        CheckConstraint("year BETWEEN 1900 AND 2199", name="scenario_year"),
        CheckConstraint(
            "(parent_scenario_id IS NULL) = (parent_revision_id IS NULL)",
            name="scenario_parent_pair",
        ),
        ForeignKeyConstraint(
            ["parent_revision_id", "parent_scenario_id"],
            ["scenario_revision.id", "scenario_revision.scenario_id"],
            name="scenario_parent_revision",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    base_snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("cost_policy_version.id"))
    fx_set_id: Mapped[str] = mapped_column(ForeignKey("fx_rate_set.id"))
    parent_scenario_id: Mapped[str | None] = mapped_column(ForeignKey("scenario.id"))
    parent_revision_id: Mapped[str | None] = mapped_column(String(36))
    year: Mapped[int] = mapped_column(Integer)
    planning_date: Mapped[date] = mapped_column(Date)
    scope_orgs: Mapped[list[str]] = mapped_column(JSONB)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    budget_minor: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(12), default="BUILDING")
    version: Mapped[int] = mapped_column(Integer, default=0)
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class ScenarioRevision(Base):
    __tablename__ = "scenario_revision"
    __table_args__ = (
        UniqueConstraint("scenario_id", "number"),
        UniqueConstraint("id", "scenario_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    operations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(String(240))
    author_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScenarioForecast(Base):
    __tablename__ = "scenario_forecast"
    revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"), primary_key=True)
    engine_version: Mapped[str] = mapped_column(String(40))
    model_version: Mapped[str] = mapped_column(String(40))
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScenarioDecision(Base):
    __tablename__ = "scenario_decision"
    __table_args__ = (
        CheckConstraint(
            "action IN ('SUBMIT','START_REVIEW','APPROVE','RETURN','PROMOTE')",
            name="scenario_decision_action",
        ),
        ForeignKeyConstraint(
            ["revision_id", "scenario_id"],
            ["scenario_revision.id", "scenario_revision.scenario_id"],
        ),
        UniqueConstraint("scenario_id", "expected_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario.id"), index=True)
    revision_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"))
    expected_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(String(240))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScenarioVerification(Base):
    __tablename__ = "scenario_verification"
    revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScenarioManifest(Base):
    __tablename__ = "scenario_manifest"
    revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlanningBaseline(Base):
    __tablename__ = "planning_baseline"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario.id"), unique=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"), unique=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("scenario_decision.id"), unique=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
