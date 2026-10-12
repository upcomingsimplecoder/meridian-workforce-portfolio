"""Validated, pure scenario transformations; source histories are never edited in place."""

import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Annotated, Any, Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, TypeAdapter, field_validator

from meridian.finance_engine import (
    CALCULATION_PRECISION,
    REGIONS,
    CostAssignment,
    FinanceInputError,
    decimal_string,
    validate_history,
)
from meridian.org_calendar import OrgCalendar
from meridian.schemas import StrictModel

OPERATION_MODEL_VERSION = "scenario-operations-v1"
MAX_OPERATIONS = 200
SyntheticID = Annotated[str, Field(pattern=r"^SYN-[0-9]{6}$")]
OrgID = Annotated[str, Field(pattern=r"^D[0-9]{2}$")]
Grade = Annotated[str, Field(pattern=r"^L(?:[1-9]|10)$")]
Minor = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


class Operation(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    effective_date: date
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("A meaningful reason is required")
        return value.strip()

    @field_validator("effective_date", mode="before")
    @classmethod
    def calendar_date(cls, value: Any) -> Any:
        if isinstance(value, date) and type(value) is date:
            return value
        if (
            isinstance(value, str)
            and len(value) == 10
            and date.fromisoformat(value).isoformat() == value
        ):
            return value
        raise ValueError("Explicit ISO calendar date required")


class Hire(Operation):
    kind: Literal["hire"]
    employee_id: SyntheticID
    position_id: str = Field(pattern=r"^POS-[0-9]{6}$")
    org_id: OrgID
    manager_id: SyntheticID | None
    grade: Grade
    annual_pay_minor: Minor
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    fte: str = Field(pattern=r"^[0-9](?:\.[0-9]{1,4})?$")


class Departure(Operation):
    kind: Literal["departure"]
    employee_id: SyntheticID


class Transfer(Operation):
    kind: Literal["transfer"]
    employee_id: SyntheticID
    org_id: OrgID
    manager_id: SyntheticID | None


class Promotion(Operation):
    kind: Literal["promotion"]
    employee_id: SyntheticID
    grade: Grade
    annual_pay_minor: Minor


class Merit(Operation):
    kind: Literal["merit_assumption"]
    org_id: OrgID
    rate: str = Field(pattern=r"^0(?:\.[0-9]{1,6})?$")


class Benefits(Operation):
    kind: Literal["benefit_multiplier"]
    org_id: OrgID
    multiplier: str = Field(pattern=r"^[1-3](?:\.[0-9]{1,6})?$")


class VacancyDelay(Operation):
    kind: Literal["vacancy_delay"]
    hire_operation_id: UUID


ScenarioOperation = Annotated[
    Hire | Departure | Transfer | Promotion | Merit | Benefits | VacancyDelay,
    Field(discriminator="kind"),
]
OperationList = TypeAdapter(list[ScenarioOperation])


@dataclass(frozen=True)
class PlanningAssignment:
    cost: CostAssignment
    position_id: str
    manager_id: str | None
    grade: str


@dataclass(frozen=True)
class PlanningRules:
    # Persist this document in each scenario before applying it in the API slice.
    hire_lead_days: int = 14

    def document(self) -> dict[str, Any]:
        return {
            "model_version": OPERATION_MODEL_VERSION,
            "hire_lead_days": self.hire_lead_days,
            "merit_rounding": "ROUND_HALF_EVEN original minor units",
            "merit_population": "active at effective date",
            "benefits_population": "all active segments including subsequent hires",
        }


def _version(op: Operation, row: CostAssignment, start: date) -> str:
    return str(uuid5(op.id, f"{row.version_id}:{start.isoformat()}"))


def _active(rows: list[PlanningAssignment], employee_id: str, day: date) -> PlanningAssignment:
    matches = [
        row for row in rows if row.cost.employee_id == employee_id and row.cost.active_at(day)
    ]
    if len(matches) != 1:
        raise FinanceInputError("OPERATION_REQUIRES_ACTIVE_EMPLOYEE")
    return matches[0]


def _modify(
    rows: list[PlanningAssignment],
    employee_id: str,
    op: Operation,
    scope: set[str],
    changes: dict[str, Any],
    *,
    grade: str | None = None,
    manager: str | None = None,
    set_manager: bool = False,
) -> list[PlanningAssignment]:
    output = []
    for row in rows:
        cost = row.cost
        if (
            cost.employee_id != employee_id
            or cost.status != "ACTIVE"
            or (cost.valid_to or date.max) <= op.effective_date
        ):
            output.append(row)
            continue
        if cost.org_id not in scope:
            raise FinanceInputError("OPERATION_CROSSES_PLANNING_SCOPE")
        if cost.valid_from < op.effective_date:
            output.append(replace(row, cost=replace(cost, valid_to=op.effective_date)))
        start = max(cost.valid_from, op.effective_date)
        changed = replace(cost, valid_from=start, version_id=_version(op, cost, start), **changes)
        output.append(
            replace(
                row,
                cost=changed,
                grade=grade or row.grade,
                manager_id=manager if set_manager else row.manager_id,
            )
        )
    return output


def _validate_graph(rows: list[PlanningAssignment], start: date, end: date) -> None:
    validate_history([row.cost for row in rows])
    boundaries = {start} | {
        day
        for row in rows
        for day in (row.cost.valid_from, row.cost.valid_to)
        if day is not None and start <= day < end
    }
    if len(boundaries) > 512:
        raise FinanceInputError("PLANNING_GRAPH_BOUNDARY_LIMIT")
    for day in sorted(boundaries):
        active = {row.cost.employee_id: row for row in rows if row.cost.active_at(day)}
        positions: set[str] = set()
        complete: set[str] = set()
        for employee, row in active.items():
            if row.position_id in positions:
                raise FinanceInputError("DUPLICATE_ACTIVE_POSITION")
            positions.add(row.position_id)
            path: set[str] = set()
            current: str | None = employee
            while current is not None and current not in complete:
                if current in path:
                    raise FinanceInputError("PLANNING_MANAGER_CYCLE")
                if current not in active:
                    raise FinanceInputError("PLANNING_MANAGER_NOT_ACTIVE")
                path.add(current)
                current = active[current].manager_id
            complete.update(path)


def apply_operations(
    source: list[PlanningAssignment],
    operations: list[ScenarioOperation],
    *,
    year: int,
    planning_date: date,
    scope_orgs: tuple[str, ...],
    org_regions: dict[str, str] | OrgCalendar,
    rules: PlanningRules,
    snapshot_source_batch_id: str,
) -> list[PlanningAssignment]:
    """Apply ordered effective operations, then validate the complete resulting history.

    Operations must be chronological; a vacancy delay explicitly retimes an earlier hire.
    Source attribution remains on each derived segment; deterministic version IDs identify
    the operation that changed it. Immutable revision JSON is the operation provenance.
    """
    if len(operations) > MAX_OPERATIONS or len({op.id for op in operations}) != len(operations):
        raise FinanceInputError("INVALID_OPERATION_COUNT_OR_DUPLICATE_ID")
    if (
        not 1900 <= year <= 2199
        or type(rules.hire_lead_days) is not int
        or not 0 <= rules.hire_lead_days <= 365
    ):
        raise FinanceInputError("INVALID_PLANNING_PERIOD_OR_RULES")
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    calendar = org_regions if isinstance(org_regions, OrgCalendar) else None
    known = org_regions.org_ids if isinstance(org_regions, OrgCalendar) else set(org_regions)

    def region_at(org: str, day: date) -> str:
        return (
            org_regions.region_at(org, day)
            if isinstance(org_regions, OrgCalendar)
            else org_regions[org]
        )

    if (
        planning_date >= end
        or not scope_orgs
        or len(set(scope_orgs)) != len(scope_orgs)
        or len(scope_orgs) > 100
        or not set(scope_orgs) <= known
        or any(
            not re.fullmatch(r"D[0-9]{2}", org)
            or region_at(org, max(start, planning_date)) not in REGIONS
            for org in scope_orgs
        )
    ):
        raise FinanceInputError("INVALID_PLANNING_SCOPE_OR_DATE")
    previous = start
    retimed = list(operations)
    for index, op in enumerate(operations):
        if not max(start, planning_date) <= op.effective_date < end:
            raise FinanceInputError("OPERATION_OUTSIDE_PLANNING_PERIOD")
        if not isinstance(op, VacancyDelay):
            if op.effective_date < previous:
                raise FinanceInputError("OPERATIONS_MUST_BE_CHRONOLOGICAL")
            previous = op.effective_date
        else:
            candidates = [
                i
                for i, earlier in enumerate(retimed[:index])
                if isinstance(earlier, Hire) and earlier.id == op.hire_operation_id
            ]
            if len(candidates) != 1:
                raise FinanceInputError("DELAY_REQUIRES_PRIOR_HIRE")
            hire_index = candidates[0]
            hire = retimed[hire_index]
            if op.effective_date <= hire.effective_date:
                raise FinanceInputError("VACANCY_DELAY_MUST_MOVE_LATER")
            retimed[hire_index] = hire.model_copy(update={"effective_date": op.effective_date})
    scope = set(scope_orgs)
    try:
        UUID(snapshot_source_batch_id)
    except ValueError as error:
        raise FinanceInputError("INVALID_PINNED_SOURCE_BATCH") from error
    rows = list(source)
    validate_history([row.cost for row in rows])
    for op in retimed:
        if isinstance(op, Hire):
            if op.org_id not in scope:
                raise FinanceInputError("OPERATION_CROSSES_PLANNING_SCOPE")
            if op.effective_date < planning_date + timedelta(days=rules.hire_lead_days):
                raise FinanceInputError("HIRE_LEAD_TIME_NOT_MET")
            if any(row.cost.employee_id == op.employee_id for row in rows):
                raise FinanceInputError("HIRE_IDENTITY_ALREADY_EXISTS")
            if any(row.position_id == op.position_id for row in rows):
                raise FinanceInputError("HIRE_POSITION_ALREADY_EXISTS")
            if (
                op.manager_id is not None
                and _active(rows, op.manager_id, op.effective_date).cost.org_id not in scope
            ):
                raise FinanceInputError("MANAGER_OUTSIDE_PLANNING_SCOPE")
            cost = CostAssignment(
                op.employee_id,
                str(op.id),
                snapshot_source_batch_id,
                op.org_id,
                region_at(op.org_id, op.effective_date),
                op.effective_date,
                None,
                "ACTIVE",
                op.annual_pay_minor,
                op.currency,
                op.fte,
            )
            rows.append(PlanningAssignment(cost, op.position_id, op.manager_id, op.grade))
        elif isinstance(op, Departure | Transfer | Promotion):
            active = _active(rows, op.employee_id, op.effective_date)
            if active.cost.org_id not in scope:
                raise FinanceInputError("OPERATION_CROSSES_PLANNING_SCOPE")
            if isinstance(op, Departure):
                rows = _modify(rows, op.employee_id, op, scope, {"status": "EXITED"})
            elif isinstance(op, Transfer):
                if op.org_id not in scope:
                    raise FinanceInputError("OPERATION_CROSSES_PLANNING_SCOPE")
                if (
                    op.manager_id is not None
                    and _active(rows, op.manager_id, op.effective_date).cost.org_id not in scope
                ):
                    raise FinanceInputError("MANAGER_OUTSIDE_PLANNING_SCOPE")
                rows = _modify(
                    rows,
                    op.employee_id,
                    op,
                    scope,
                    {"org_id": op.org_id, "region": region_at(op.org_id, op.effective_date)},
                    manager=op.manager_id,
                    set_manager=True,
                )
            else:
                if int(op.grade[1:]) <= int(active.grade[1:]):
                    raise FinanceInputError("PROMOTION_REQUIRES_HIGHER_GRADE")
                if int(op.annual_pay_minor) < int(active.cost.annual_pay_minor):
                    raise FinanceInputError("PROMOTION_CANNOT_REDUCE_SALARY")
                rows = _modify(
                    rows,
                    op.employee_id,
                    op,
                    scope,
                    {"annual_pay_minor": op.annual_pay_minor},
                    grade=op.grade,
                )
        elif isinstance(op, Merit):
            if op.org_id not in scope:
                raise FinanceInputError("OPERATION_CROSSES_PLANNING_SCOPE")
            rate = decimal_string(op.rate, maximum="0.25", places=6)
            population = {
                row.cost.employee_id
                for row in rows
                if row.cost.org_id == op.org_id and row.cost.active_at(op.effective_date)
            }
            # One pass preserves future source changes without O(people²) rescans.
            changed = []
            with localcontext(Context(prec=CALCULATION_PRECISION, rounding=ROUND_HALF_EVEN)):
                for row in rows:
                    if (
                        row.cost.employee_id not in population
                        or row.cost.status != "ACTIVE"
                        or (row.cost.valid_to or date.max) <= op.effective_date
                    ):
                        changed.append(row)
                        continue
                    pay = (Decimal(row.cost.annual_pay_minor) * (1 + rate)).quantize(Decimal(1))
                    changed.extend(
                        _modify(
                            [row],
                            row.cost.employee_id,
                            op,
                            scope,
                            {"annual_pay_minor": format(pay, "f")},
                        )
                    )
            rows = changed
        elif isinstance(op, Benefits):
            if op.org_id not in scope:
                raise FinanceInputError("OPERATION_CROSSES_PLANNING_SCOPE")
            decimal_string(op.multiplier, minimum="1", maximum="3", places=6)
        # Delays have already retimed their referenced hires; no synthetic departure/rehire.
    # Benefits are an ongoing department assumption, including hires created later in the log.
    for op in retimed:
        if not isinstance(op, Benefits):
            continue
        changed = []
        for row in rows:
            if (
                row.cost.org_id != op.org_id
                or row.cost.status != "ACTIVE"
                or (row.cost.valid_to or date.max) <= op.effective_date
            ):
                changed.append(row)
            else:
                changed.extend(
                    _modify(
                        [row],
                        row.cost.employee_id,
                        op,
                        scope,
                        {"benefit_multiplier": op.multiplier},
                    )
                )
        rows = changed
    if calendar:
        # Coverage is required throughout every active segment in the forecast.
        # Only hire/transfer-derived region changes follow future organization changes;
        # original source regions remain the original employee-location evidence.
        region_changes: dict[str, date] = {}
        for op in retimed:
            if isinstance(op, Hire | Transfer):
                region_changes[op.employee_id] = min(
                    region_changes.get(op.employee_id, op.effective_date), op.effective_date
                )
        expanded = []
        for row in rows:
            cost = row.cost
            left, right = max(start, cost.valid_from), min(end, cost.valid_to or end)
            if cost.status != "ACTIVE" or left >= right:
                expanded.append(row)
                continue
            segments = calendar.segments(cost.org_id, left, right)
            if (
                cost.employee_id not in region_changes
                or cost.valid_from < region_changes[cost.employee_id]
            ):
                expanded.append(row)
                continue
            if cost.valid_from < left:
                expanded.append(replace(row, cost=replace(cost, valid_to=left)))
            for segment_start, segment_end, region in segments:
                expanded.append(
                    replace(
                        row,
                        cost=replace(
                            cost,
                            version_id=str(
                                uuid5(
                                    UUID(cost.version_id), f"org-calendar:{segment_start}:{region}"
                                )
                            ),
                            valid_from=segment_start,
                            valid_to=segment_end if segment_end < right else cost.valid_to,
                            region=region,
                        ),
                    )
                )
        rows = expanded
    _validate_graph(rows, start, end)
    return sorted(rows, key=lambda row: (row.cost.employee_id, row.cost.valid_from))


def validate_budget(forecast: dict[str, Any], budget_minor: str) -> None:
    decimal_string(budget_minor, maximum="99999999999999999999", places=0)
    if int(forecast["annual"]["total_minor"]) > int(budget_minor):
        raise FinanceInputError("FORECAST_EXCEEDS_PINNED_BUDGET")
