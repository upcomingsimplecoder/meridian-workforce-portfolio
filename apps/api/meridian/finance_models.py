"""Sealed financial assumptions. No mutable 'current rate' may enter a calculation."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now


class CostPolicyVersion(Base):
    __tablename__ = "cost_policy_version"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="cost_policy_sha"),
        CheckConstraint("jsonb_typeof(definition) = 'object'", name="cost_policy_object"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_label: Mapped[str] = mapped_column(String(100))
    effective_date: Mapped[date] = mapped_column(Date)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FxRateSet(Base):
    __tablename__ = "fx_rate_set"
    __table_args__ = (
        CheckConstraint("effective_start < effective_end", name="fx_valid_period"),
        CheckConstraint("state IN ('BUILDING','SEALED')", name="fx_set_state"),
        CheckConstraint(
            "reporting_currency IN ('USD','EUR','GBP','JPY','KWD')", name="fx_quote_currency"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="fx_set_sha"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_label: Mapped[str] = mapped_column(String(100))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_start: Mapped[date] = mapped_column(Date)
    effective_end: Mapped[date] = mapped_column(Date)
    reporting_currency: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(10))
    sha256: Mapped[str] = mapped_column(String(64))


class FxRate(Base):
    __tablename__ = "fx_rate"
    __table_args__ = (
        CheckConstraint("rate_decimal > 0", name="fx_rate_positive"),
        CheckConstraint(
            "source_currency IN ('USD','EUR','GBP','JPY','KWD')", name="fx_source_currency"
        ),
    )
    set_id: Mapped[str] = mapped_column(ForeignKey("fx_rate_set.id"), primary_key=True)
    source_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(3))
    rate_decimal: Mapped[Decimal] = mapped_column(Numeric(24, 12))
