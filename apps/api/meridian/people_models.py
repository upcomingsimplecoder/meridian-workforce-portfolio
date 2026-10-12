from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now, uid


class AuthzPolicy(Base):
    __tablename__ = "authz_policy_version"
    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceBatch(Base):
    __tablename__ = "source_batch"
    __table_args__ = (UniqueConstraint("source_type", "sha256"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_type: Mapped[str] = mapped_column(String(30))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))
    accepted_count: Mapped[int] = mapped_column(Integer)
    rejected_count: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark: Mapped[date] = mapped_column(Date)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)


class Snapshot(Base):
    __tablename__ = "snapshot"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_batch_id: Mapped[str] = mapped_column(ForeignKey("source_batch.id"), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    source_watermark: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="BUILDING")


class OrgUnit(Base):
    __tablename__ = "org_unit"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    cost_center: Mapped[str] = mapped_column(String(20))


class OrgVersion(Base):
    __tablename__ = "org_version"
    __table_args__ = (
        ExcludeConstraint(
            ("snapshot_id", "="),
            ("org_id", "="),
            (text("daterange(valid_from,valid_to,'[)')"), "&&"),
            name="org_version_no_overlap",
            using="gist",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("org_unit.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("org_unit.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(30))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)


class Employee(Base):
    __tablename__ = "employee"
    __table_args__ = (CheckConstraint("id ~ '^SYN-[0-9]{6}$'", name="employee_synthetic_id"),)
    id: Mapped[str] = mapped_column(String(10), primary_key=True)


class Position(Base):
    __tablename__ = "position"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)


class EmployeeVersion(Base):
    __tablename__ = "employee_version"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "employee_id", "valid_from"),
        CheckConstraint("fte > 0 AND fte <= 1", name="employee_fte"),
        CheckConstraint("annual_pay_minor >= 0", name="employee_nonnegative_pay"),
        CheckConstraint("currency IN ('USD','EUR','GBP','JPY','KWD')", name="employee_currency"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="employee_valid_interval"
        ),
        ExcludeConstraint(
            ("snapshot_id", "="),
            ("employee_id", "="),
            (text("daterange(valid_from,valid_to,'[)')"), "&&"),
            name="employee_version_no_overlap",
            using="gist",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id"), index=True)
    position_id: Mapped[str] = mapped_column(ForeignKey("position.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    org_id: Mapped[str] = mapped_column(ForeignKey("org_unit.id"), index=True)
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("employee.id"), index=True)
    job_family: Mapped[str] = mapped_column(String(50))
    grade: Mapped[str] = mapped_column(String(10))
    region: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    annual_pay_minor: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    currency: Mapped[str] = mapped_column(String(3))
    fte: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    valid_to: Mapped[date | None] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source_batch_id: Mapped[str] = mapped_column(ForeignKey("source_batch.id"), index=True)


class PrincipalEmployee(Base):
    __tablename__ = "principal_employee"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id"), unique=True)
