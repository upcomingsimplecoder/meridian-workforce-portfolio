"""Deterministic synthetic compensation rules; no database, personnel scoring or AI.

Merit is a full-time annual increment, bonus a full-time/full-service one-time target.
Amounts and every rounded output remain original-currency integer minor units.
See ADR-016 for the distinct pool, first-year and FX reporting conventions.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.finance_engine import (
    CALCULATION_PRECISION,
    EXPONENTS,
    CostAssignment,
    FinanceInputError,
    RateSet,
    boundaries,
    decimal_string,
)
from meridian.policy import canonical_hash

ENGINE_VERSION = "meridian-comp-1.0.0"
MODEL_VERSION: Literal["annualized-merit-service-bonus-v1"] = "annualized-merit-service-bonus-v1"
MAX_POPULATION = 5000
MAX_MINOR = 10**20 - 1


class Guideline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    methodology: Literal["annualized-merit-service-bonus-v1"] = MODEL_VERSION
    timezone: Literal["America/New_York"] = "America/New_York"
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"
    merit_basis: Literal["full-time annual original-currency increment"] = (
        "full-time annual original-currency increment"
    )
    bonus_basis: Literal["full-time full-service one-time target"] = (
        "full-time full-service one-time target"
    )
    effective_date: date
    minimum_service_days: int = Field(default=30, ge=0, le=366)
    merit_guideline_rate: str = "0.04"
    merit_hard_cap_rate: str = "0.20"
    bonus_guideline_rate: str = "0.10"
    bonus_hard_cap_rate: str = "0.50"

    @model_validator(mode="after")
    def rates(self) -> "Guideline":
        for guideline, cap in (
            (self.merit_guideline_rate, self.merit_hard_cap_rate),
            (self.bonus_guideline_rate, self.bonus_hard_cap_rate),
        ):
            limit = decimal_string(cap, maximum="1", places=6)
            if decimal_string(guideline, maximum="1", places=6) > limit:
                raise FinanceInputError("GUIDELINE_EXCEEDS_HARD_CAP")
        return self

    def document(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def sha256(self) -> str:
        return canonical_hash(self.document())


class CycleTerms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    eligibility_date: date
    merit_effective_date: date
    bonus_service_start: date
    bonus_service_end: date
    bonus_payment_date: date

    @model_validator(mode="after")
    def chronology(self) -> "CycleTerms":
        days = (self.bonus_service_end - self.bonus_service_start).days
        if (
            not 1 <= days <= 366
            or not 1900 <= self.merit_effective_date.year <= 2199
            or any(
                not 1900 <= day.year <= 2200
                for day in (
                    self.eligibility_date,
                    self.bonus_service_start,
                    self.bonus_service_end,
                    self.bonus_payment_date,
                )
            )
            or self.eligibility_date > self.merit_effective_date
            or self.bonus_payment_date < self.bonus_service_end
            or self.bonus_payment_date < self.eligibility_date
            or self.eligibility_date < self.bonus_service_start
            or self.eligibility_date >= self.bonus_service_end
        ):
            raise FinanceInputError("INVALID_COMPENSATION_CHRONOLOGY")
        return self

    def document(self) -> dict[str, str]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class ChainLink:
    employee_id: str
    version_id: str
    manager_id: str | None
    source_batch_id: str

    def document(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "version_id": self.version_id,
            "manager_id": self.manager_id,
            "source_batch_id": self.source_batch_id,
        }


@dataclass(frozen=True)
class FrozenEligibility:
    employee_id: str
    eligible: bool
    reason_codes: tuple[str, ...]
    org_id: str | None
    currency: str | None
    base_pay_minor: str | None
    fte: str | None
    proposer_id: str | None
    service_days: int
    history: tuple[CostAssignment, ...]
    chain: tuple[ChainLink, ...]

    def document(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "org_id": self.org_id,
            "currency": self.currency,
            "base_pay_minor": self.base_pay_minor,
            "fte": self.fte,
            "proposer_id": self.proposer_id,
            "service_days": self.service_days,
            "history": [row.fingerprint() for row in self.history],
            "chain": [link.document() for link in self.chain],
        }

    @property
    def sha256(self) -> str:
        return canonical_hash(self.document())


def minor_string(value: str, *, maximum_digits: int = 20) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        rf"0|[1-9][0-9]{{0,{maximum_digits - 1}}}", value
    ):
        raise FinanceInputError("INVALID_COMPENSATION_MINOR_UNITS")
    return int(value)


def round_ratio(numerator: Decimal, denominator: int = 1) -> int:
    """One exact half-even rounding at the named output boundary, without division drift."""
    quotient, remainder = divmod(numerator, Decimal(denominator))
    integer = int(quotient)
    doubled = remainder * 2
    return integer + int(doubled > denominator or (doubled == denominator and integer % 2 == 1))


def _active_overlap(row: CostAssignment, start: date, end: date) -> int:
    if row.status != "ACTIVE":
        return 0
    return max(0, (min(row.valid_to or date.max, end) - max(row.valid_from, start)).days)


def active_service_days(history: tuple[CostAssignment, ...], as_of: date) -> int:
    # Eligibility counts calendar service, never FTE twice or projected future service.
    # A bounded trailing 366-day window satisfies every supported minimum-service policy.
    start = as_of - timedelta(days=365)
    return sum(_active_overlap(row, start, as_of + timedelta(days=1)) for row in history)


def _merit_first_year(
    history: tuple[CostAssignment, ...], terms: CycleTerms, merit: int, currency: str
) -> tuple[dict[str, str], ...]:
    months: list[dict[str, str]] = []
    for month in range(1, 13):
        start, end = boundaries(terms.merit_effective_date.year, month)
        numerator = Decimal(0)
        for row in history:
            active_days = _active_overlap(row, max(start, terms.merit_effective_date), end)
            if active_days:
                if row.currency != currency:
                    raise FinanceInputError("COMPENSATION_CURRENCY_CHANGE_IN_MERIT_PERIOD")
                numerator += Decimal(merit) * Decimal(row.fte) * active_days
        months.append(
            {
                "month": start.strftime("%Y-%m"),
                "merit_cost_minor": str(
                    round_ratio(numerator, 12 * calendar.monthrange(start.year, month)[1])
                ),
            }
        )
    return tuple(months)


def calculate_award(
    eligibility: FrozenEligibility,
    guideline: Guideline,
    terms: CycleTerms,
    fx: RateSet,
    *,
    merit_minor: str,
    bonus_minor: str,
) -> dict[str, Any]:
    """Classify business blockers/exceptions/warnings; malformed or corrupt pins fail closed.

    The caller must obtain eligibility from the sealed snapshot adapter. This pure function
    does not confer permissions; field/object authorization belongs to each API projection.
    """
    merit, bonus = minor_string(merit_minor), minor_string(bonus_minor)
    if guideline.effective_date > terms.eligibility_date:
        raise FinanceInputError("COMPENSATION_GUIDELINE_NOT_EFFECTIVE")
    if not eligibility.eligible:
        raise FinanceInputError("COMPENSATION_EMPLOYEE_INELIGIBLE")
    if (
        eligibility.org_id is None
        or eligibility.currency not in EXPONENTS
        or eligibility.base_pay_minor is None
        or eligibility.fte is None
        or eligibility.proposer_id is None
    ):
        raise FinanceInputError("CORRUPT_COMPENSATION_ELIGIBILITY")
    currency = eligibility.currency
    period_end = max(
        date(terms.merit_effective_date.year + 1, 1, 1),
        terms.bonus_payment_date + timedelta(days=1),
    )
    rates = fx.validate(min(terms.merit_effective_date, terms.bonus_service_start), period_end)
    if currency not in rates:
        raise FinanceInputError("MISSING_PINNED_FX_RATE")
    with localcontext(Context(prec=CALCULATION_PRECISION, rounding=ROUND_HALF_EVEN)):
        base = minor_string(eligibility.base_pay_minor)
        blockers: list[str] = []
        exceptions: list[str] = []
        warnings: list[str] = []
        if not merit and not bonus:
            warnings.append("ZERO_AWARD")
        for kind, amount, soft, hard in (
            ("MERIT", merit, guideline.merit_guideline_rate, guideline.merit_hard_cap_rate),
            ("BONUS", bonus, guideline.bonus_guideline_rate, guideline.bonus_hard_cap_rate),
        ):
            if amount > Decimal(base) * Decimal(hard):
                blockers.append(f"{kind}_HARD_CAP_EXCEEDED")
            elif amount > Decimal(base) * Decimal(soft):
                exceptions.append(f"{kind}_GUIDELINE_EXCEPTION")
        effective = next(
            (row for row in eligibility.history if row.active_at(terms.merit_effective_date)),
            None,
        )
        if merit and effective is None:
            blockers.append("INACTIVE_ON_MERIT_EFFECTIVE_DATE")
        if effective is not None and effective.currency != currency:
            raise FinanceInputError("COMPENSATION_CURRENCY_CHANGE_AT_MERIT_DATE")
        if any(
            minor_string(row.annual_pay_minor) + merit > MAX_MINOR
            for row in eligibility.history
            if _active_overlap(
                row, terms.merit_effective_date, date(terms.merit_effective_date.year + 1, 1, 1)
            )
        ):
            blockers.append("RESULTING_BASE_PAY_OVERFLOW")
        # Pool allocation is annualized at the fixed award effective date. First-year
        # impact separately follows known frozen employment/FTE changes month by month.
        annualized_merit = round_ratio(Decimal(merit) * Decimal(effective.fte)) if effective else 0
        service_fte_days = sum(
            (
                Decimal(_active_overlap(row, terms.bonus_service_start, terms.bonus_service_end))
                * Decimal(row.fte)
                for row in eligibility.history
            ),
            Decimal(0),
        )
        bonus_payable = round_ratio(
            Decimal(bonus) * service_fte_days,
            (terms.bonus_service_end - terms.bonus_service_start).days,
        )
        months = _merit_first_year(eligibility.history, terms, merit, currency)
        charge = annualized_merit + bonus_payable
        # Convert the rounded original-currency person charge once. Pool reporting sums
        # these person amounts; it does not re-convert pooled totals and hide residuals.
        reporting = round_ratio(
            Decimal(charge) * rates[currency] * 10 ** EXPONENTS[fx.reporting_currency],
            10 ** EXPONENTS[currency],
        )
        document: dict[str, Any] = {
            "engine_version": ENGINE_VERSION,
            "model_version": MODEL_VERSION,
            "employee_id": eligibility.employee_id,
            "org_id": eligibility.org_id,
            "currency": currency,
            "currency_exponent": EXPONENTS[currency],
            "merit_minor": merit_minor,
            "merit_reference_base_pay_minor": str(base),
            "effective_base_pay_minor": effective.annual_pay_minor if effective else None,
            "resulting_base_pay_minor": str(int(effective.annual_pay_minor) + merit)
            if effective
            else None,
            "bonus_target_minor": bonus_minor,
            "annualized_merit_minor": str(annualized_merit),
            "merit_first_year_minor": str(sum(int(month["merit_cost_minor"]) for month in months)),
            "merit_months": list(months),
            "bonus_service_fte_days": format(service_fte_days, "f"),
            "bonus_service_calendar_days": (
                terms.bonus_service_end - terms.bonus_service_start
            ).days,
            "bonus_payable_minor": str(bonus_payable),
            "pool_charge_minor": str(charge),
            "reporting_currency": fx.reporting_currency,
            "reporting_pool_charge_minor": str(reporting),
            "blockers": blockers,
            "exceptions": exceptions,
            "warnings": warnings,
            "inputs": {
                "eligibility_sha256": eligibility.sha256,
                "guideline_sha256": guideline.sha256,
                "fx_set_id": fx.id,
                "fx_sha256": fx.sha256,
                "terms": terms.document(),
                "terms_sha256": canonical_hash(terms.document()),
            },
        }
        return document | {"sha256": canonical_hash(document)}


@dataclass(frozen=True)
class Pool:
    org_id: str
    currency: str
    cost_center: str
    budget_minor: str
    holdback_minor: str

    def validate(self) -> None:
        budget, holdback = minor_string(self.budget_minor), minor_string(self.holdback_minor)
        if (
            not re.fullmatch(r"D[0-9]{2}", self.org_id)
            or self.currency not in EXPONENTS
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", self.cost_center)
            or holdback > budget
        ):
            raise FinanceInputError("INVALID_COMPENSATION_POOL")

    def document(self) -> dict[str, str]:
        self.validate()
        return {
            "org_id": self.org_id,
            "currency": self.currency,
            "cost_center": self.cost_center,
            "budget_minor": self.budget_minor,
            "holdback_minor": self.holdback_minor,
        }


def evaluate_pool(pool: Pool, awards: list[dict[str, Any]]) -> dict[str, Any]:
    """All drafts reserve funds; a holdback release is governance, overspend a blocker."""
    pool.validate()
    if len(awards) > MAX_POPULATION:
        raise FinanceInputError("COMPENSATION_POOL_WORK_LIMIT")
    people: set[str] = set()
    for award in awards:
        if award["org_id"] != pool.org_id or award["currency"] != pool.currency:
            raise FinanceInputError("COMPENSATION_AWARD_POOL_MISMATCH")
        if award["employee_id"] in people:
            raise FinanceInputError("DUPLICATE_COMPENSATION_POOL_AWARD")
        if award["sha256"] != canonical_hash({k: v for k, v in award.items() if k != "sha256"}):
            raise FinanceInputError("COMPENSATION_AWARD_FINGERPRINT_MISMATCH")
        minor_string(award["pool_charge_minor"], maximum_digits=21)
        people.add(award["employee_id"])
    total = sum(int(award["pool_charge_minor"]) for award in awards)
    budget, holdback = int(pool.budget_minor), int(pool.holdback_minor)
    available = budget - holdback
    return pool.document() | {
        "award_count": len(awards),
        "utilized_minor": str(total),
        "available_without_holdback_minor": str(available),
        "remaining_total_minor": str(budget - total),
        "holdback_required_minor": str(min(holdback, max(0, total - available))),
        "blockers": ["POOL_BUDGET_EXCEEDED"] if total > budget else [],
        "exceptions": ["POOL_HOLDBACK_RELEASE"] if available < total <= budget else [],
        "warnings": ["POOL_AT_BUDGET"] if total == budget else [],
    }
