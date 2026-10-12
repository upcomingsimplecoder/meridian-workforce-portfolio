"""Pure Decimal engine: immutable inputs, explicit currency/dates, additive rounded outputs.

No database/network/global mutable state is read during a calculation. See ADR-013.
"""

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

from meridian.policy import canonical_hash

ENGINE_VERSION = "meridian-cost-1.0.0"
MODEL_VERSION = "monthly-calendar-v1"
EXPONENTS = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KWD": 3}
REGIONS = frozenset({"Americas", "Europe", "Asia Pacific", "Middle East"})
COMPONENTS = ("base", "benefits", "variable", "employer_tax")
ZERO = Decimal(0)
ONE = Decimal(1)
MAX_HISTORY_ROWS = 100000
CALCULATION_PRECISION = 96


class FinanceInputError(ValueError):
    """Safe deterministic error code, never includes a source salary or rejected value."""


def decimal_string(value: str, *, maximum: str, places: int = 12, minimum: str = "0") -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,24}(?:\.[0-9]{1,12})?", value):
        raise FinanceInputError("INVALID_DECIMAL_STRING")
    result = Decimal(value)
    exponent = result.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or not Decimal(minimum) <= result <= Decimal(maximum)
        or exponent < -places
    ):
        raise FinanceInputError("DECIMAL_OUT_OF_RANGE")
    return result


@dataclass(frozen=True)
class CostPolicy:
    id: str
    effective_date: date
    timezone: str
    # Tuples prevent later mutation of an input after its fingerprint has been calculated.
    benefits: tuple[tuple[str, str], ...]
    variable_rate: str
    employer_tax_rate: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "benefits", tuple(sorted(self.benefits)))

    def document(self) -> dict[str, Any]:
        return {
            "methodology": MODEL_VERSION,
            "effective_date": self.effective_date.isoformat(),
            "timezone": self.timezone,
            "rounding": "ROUND_HALF_EVEN",
            "department_allocation": "person-month largest remainder; org ID breaks ties",
            "benefit_multipliers": dict(self.benefits),
            "variable_accrual_rate": self.variable_rate,
            "employer_tax_rate": self.employer_tax_rate,
        }

    @property
    def sha256(self) -> str:
        return canonical_hash(self.document())

    def validate(self, start: date) -> None:
        if self.effective_date > start or self.timezone != "America/New_York":
            raise FinanceInputError("POLICY_OUTSIDE_SUPPORTED_PERIOD_OR_TIMEZONE")
        if len(self.benefits) != len(REGIONS) or set(dict(self.benefits)) != REGIONS:
            raise FinanceInputError("MISSING_REGION_POLICY")
        for _, multiplier in self.benefits:
            decimal_string(multiplier, minimum="1", maximum="3", places=6)
        decimal_string(self.variable_rate, maximum="1", places=6)
        decimal_string(self.employer_tax_rate, maximum="1", places=6)


@dataclass(frozen=True)
class RateSet:
    id: str
    effective_start: date
    effective_end: date
    reporting_currency: str
    rates: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rates", tuple(sorted(self.rates)))

    def document(self) -> dict[str, Any]:
        return {
            "effective_start": self.effective_start.isoformat(),
            "effective_end": self.effective_end.isoformat(),
            "reporting_currency": self.reporting_currency,
            "rates": dict(self.rates),
            "direction": "reporting major units per original major unit",
        }

    @property
    def sha256(self) -> str:
        return canonical_hash(self.document())

    def validate(self, start: date, end: date) -> dict[str, Decimal]:
        if self.effective_start > start or self.effective_end < end:
            raise FinanceInputError("FX_DOES_NOT_COVER_FORECAST_PERIOD")
        if self.reporting_currency not in EXPONENTS or len(dict(self.rates)) != len(self.rates):
            raise FinanceInputError("INVALID_FX_SET")
        rates = {
            currency: decimal_string(rate, minimum="0.000000000001", maximum="999999999999")
            for currency, rate in self.rates
        }
        if any(currency not in EXPONENTS for currency in rates):
            raise FinanceInputError("UNSUPPORTED_FX_CURRENCY")
        if rates.get(self.reporting_currency) != ONE:
            raise FinanceInputError("REPORTING_CURRENCY_REQUIRES_IDENTITY_RATE")
        return rates


@dataclass(frozen=True)
class CostAssignment:
    employee_id: str
    version_id: str
    source_batch_id: str
    org_id: str
    region: str
    valid_from: date
    valid_to: date | None
    status: str
    annual_pay_minor: str
    currency: str
    fte: str
    benefit_multiplier: str | None = None

    def active_at(self, day: date) -> bool:
        return self.status == "ACTIVE" and self.valid_from <= day < (self.valid_to or date.max)

    def fingerprint(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "version_id": self.version_id,
            "source_batch_id": self.source_batch_id,
            "org_id": self.org_id,
            "region": self.region,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "status": self.status,
            "annual_pay_minor": self.annual_pay_minor,
            "currency": self.currency,
            "fte": self.fte,
        } | (
            {"benefit_multiplier": self.benefit_multiplier}
            if self.benefit_multiplier is not None
            else {}
        )

    def validate(self) -> None:
        if not re.fullmatch(r"SYN-[0-9]{6}", self.employee_id):
            raise FinanceInputError("NON_SYNTHETIC_IDENTITY")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise FinanceInputError("INVALID_ASSIGNMENT_PERIOD")
        if self.currency not in EXPONENTS or self.region not in REGIONS:
            raise FinanceInputError("UNSUPPORTED_CURRENCY_OR_REGION")
        if not re.fullmatch(r"D[0-9]{2}", self.org_id):
            raise FinanceInputError("INVALID_ASSIGNMENT_ORGANIZATION")
        if self.status not in {"ACTIVE", "EXITED"}:
            raise FinanceInputError("INVALID_EMPLOYMENT_STATUS")
        decimal_string(self.annual_pay_minor, maximum="99999999999999999999", places=0)
        decimal_string(self.fte, minimum="0.0001", maximum="1", places=4)
        if self.benefit_multiplier is not None:
            decimal_string(self.benefit_multiplier, minimum="1", maximum="3", places=6)


def validate_history(rows: list[CostAssignment]) -> None:
    if len(rows) > MAX_HISTORY_ROWS:
        raise FinanceInputError("FORECAST_HISTORY_WORK_LIMIT")
    previous: CostAssignment | None = None
    for row in sorted(rows, key=lambda item: (item.employee_id, item.valid_from)):
        row.validate()
        if previous and previous.employee_id == row.employee_id:
            if row.valid_from < (previous.valid_to or date.max):
                raise FinanceInputError("OVERLAPPING_COST_ASSIGNMENTS")
        previous = row


def boundaries(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)


def _round_ratio(numerator: Decimal, denominator: int, exponent: int) -> int:
    # Exact quotient/remainder avoids a repeating Decimal division before rounding.
    # A split 1/31 + 30/31 must produce the same half-even tie as a whole month.
    quotient, remainder = divmod(numerator * Decimal(10) ** exponent, Decimal(denominator))
    integer = int(quotient)
    doubled = remainder * 2
    return integer + int(doubled > denominator or (doubled == denominator and integer % 2 == 1))


def _money(values: dict[str, int]) -> dict[str, str]:
    return {f"{component}_minor": str(values.get(component, 0)) for component in COMPONENTS} | {
        "total_minor": str(sum(values.get(component, 0) for component in COMPONENTS))
    }


def calculate_forecast(
    assignments: list[CostAssignment],
    policy: CostPolicy,
    fx: RateSet,
    *,
    snapshot_id: str,
    snapshot_sha256: str,
    year: int,
    scope_orgs: tuple[str, ...],
) -> dict[str, Any]:
    """Calculate a full calendar year; scope is fixed by the caller's authorized snapshot.

    Transfer-month rounding uses deterministic largest-remainder allocation per component:
    person totals round once, then each department receives its floor plus residual cents
    in descending fractional-remainder order (department ID breaks ties). This preserves
    both person-level rounding and exact department/month reconciliation.
    """
    if year < 1900 or year > 2199:
        raise FinanceInputError("UNSUPPORTED_FORECAST_YEAR")
    if not scope_orgs or len(set(scope_orgs)) != len(scope_orgs) or len(scope_orgs) > 100:
        raise FinanceInputError("INVALID_FORECAST_SCOPE")
    if any(not re.fullmatch(r"D[0-9]{2}", org) for org in scope_orgs):
        raise FinanceInputError("INVALID_FORECAST_SCOPE")
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    policy.validate(start)
    rates = fx.validate(start, end)
    validate_history(assignments)
    # Include the full transfer history for people who touch the requested scope.
    # Round once across all departments, then project the authorized department amounts.
    # This makes independently queried department totals reconcile with the whole company.
    people = {
        row.employee_id
        for row in assignments
        if row.org_id in scope_orgs
        and row.status == "ACTIVE"
        and row.valid_from < end
        and (row.valid_to or date.max) > start
    }
    scoped = [row for row in assignments if row.employee_id in people]
    if any(
        row.currency not in rates
        for row in scoped
        if row.status == "ACTIVE" and row.valid_from < end and (row.valid_to or date.max) > start
    ):
        raise FinanceInputError("MISSING_PINNED_FX_RATE")
    with localcontext(Context(prec=CALCULATION_PRECISION, rounding=ROUND_HALF_EVEN)):
        return _calculate(scoped, policy, fx, rates, snapshot_id, snapshot_sha256, year, scope_orgs)


def _allocate_departments(
    values: dict[str, Decimal], rounded_person_minor: int, exponent: int, denominator: int
) -> dict[str, int]:
    scale = Decimal(10) ** exponent
    parts = {org: divmod(value * scale, Decimal(denominator)) for org, value in values.items()}
    floors = {org: int(part[0]) for org, part in parts.items()}
    residual = rounded_person_minor - sum(floors.values())
    # All costs are nonnegative, so int() is the exact floor here.
    ranked = sorted(values, key=lambda org: (-parts[org][1], org))
    if not 0 <= residual <= len(ranked):
        raise FinanceInputError("ROUNDING_RECONCILIATION_FAILED")
    for org in ranked[:residual]:
        floors[org] += 1
    return floors


def _calculate(
    scoped: list[CostAssignment],
    policy: CostPolicy,
    fx: RateSet,
    rates: dict[str, Decimal],
    snapshot_id: str,
    snapshot_sha256: str,
    year: int,
    scope_orgs: tuple[str, ...],
) -> dict[str, Any]:
    benefits = {region: Decimal(multiplier) - ONE for region, multiplier in policy.benefits}
    variable_rate, tax_rate = Decimal(policy.variable_rate), Decimal(policy.employer_tax_rate)
    exponent = EXPONENTS[fx.reporting_currency]
    monthly = []
    annual = {component: 0 for component in COMPONENTS}
    for month in range(1, 13):
        month_start, month_end = boundaries(year, month)
        month_days = (month_end - month_start).days
        closing_day = date(year, month, calendar.monthrange(year, month)[1])
        # Per person/component/org unrounded reporting major units.
        amounts: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: ZERO)
        )
        opening: dict[str, Decimal] = {}
        closing: dict[str, Decimal] = {}
        fte_days = ZERO
        org_fte_days: dict[str, Decimal] = defaultdict(lambda: ZERO)
        org_open: dict[str, dict[str, Decimal]] = defaultdict(dict)
        org_close: dict[str, dict[str, Decimal]] = defaultdict(dict)
        for row in scoped:
            if row.status != "ACTIVE":
                continue
            left, right = max(row.valid_from, month_start), min(row.valid_to or date.max, month_end)
            if left >= right:
                continue
            fte = Decimal(row.fte)
            if row.org_id in scope_orgs and row.active_at(month_start):
                opening[row.employee_id] = fte
                org_open[row.org_id][row.employee_id] = fte
            if row.org_id in scope_orgs and row.active_at(closing_day):
                closing[row.employee_id] = fte
                org_close[row.org_id][row.employee_id] = fte
            days = Decimal((right - left).days)
            if row.org_id in scope_orgs:
                fte_days += fte * days
                org_fte_days[row.org_id] += fte * days
            # All segments share the denominator 12 * month_days. Sum exact finite
            # numerators before division so segmentation cannot change half-even ties.
            base = (
                Decimal(row.annual_pay_minor) / Decimal(10) ** EXPONENTS[row.currency] * days * fte
            )
            original = {
                "base": base,
                "benefits": base
                * (
                    Decimal(row.benefit_multiplier) - ONE
                    if row.benefit_multiplier is not None
                    else benefits[row.region]
                ),
                "variable": base * variable_rate,
                "employer_tax": base * tax_rate,
            }
            for component, cost in original.items():
                amounts[(row.employee_id, component)][row.org_id] += cost * rates[row.currency]
        totals = {component: 0 for component in COMPONENTS}
        departments: dict[str, dict[str, int]] = {
            org: {component: 0 for component in COMPONENTS} for org in sorted(scope_orgs)
        }
        for (_, component), values in sorted(amounts.items()):
            denominator = 12 * month_days
            rounded = _round_ratio(sum(values.values(), ZERO), denominator, exponent)
            allocation = _allocate_departments(values, rounded, exponent, denominator)
            for org, allocated in allocation.items():
                if org in departments:
                    totals[component] += allocated
                    departments[org][component] += allocated
        for component in COMPONENTS:
            annual[component] += totals[component]
        monthly.append(
            {
                "month": month_start.isoformat(),
                "opening_headcount": len(opening),
                "closing_headcount": len(closing),
                "opening_fte": format(sum(opening.values(), ZERO), ".4f"),
                "closing_fte": format(sum(closing.values(), ZERO), ".4f"),
                "average_fte": format(fte_days / month_days, ".4f"),
                **_money(totals),
                "departments": [
                    {
                        "org_id": org,
                        "opening_headcount": len(org_open[org]),
                        "closing_headcount": len(org_close[org]),
                        "opening_fte": format(sum(org_open[org].values(), ZERO), ".4f"),
                        "closing_fte": format(sum(org_close[org].values(), ZERO), ".4f"),
                        "average_fte": format(org_fte_days[org] / month_days, ".4f"),
                        **_money(values),
                    }
                    for org, values in departments.items()
                ],
            }
        )
    inputs = {
        "snapshot_id": snapshot_id,
        "snapshot_sha256": snapshot_sha256,
        "assignment_sha256": canonical_hash(
            [
                row.fingerprint()
                for row in sorted(scoped, key=lambda row: (row.employee_id, row.valid_from))
            ]
        ),
        "policy_id": policy.id,
        "policy_sha256": policy.sha256,
        "fx_set_id": fx.id,
        "fx_sha256": fx.sha256,
        "scope_orgs": sorted(scope_orgs),
        "year": year,
    }
    output = {
        "engine_version": ENGINE_VERSION,
        "model_version": MODEL_VERSION,
        "currency": fx.reporting_currency,
        "exponent": exponent,
        "inputs": inputs,
        "months": monthly,
        "annual": _money(annual),
    }
    return output | {"sha256": canonical_hash(output)}
