"""Freeze effective source hierarchy and transparent eligibility from sealed history."""

from collections import defaultdict

from meridian.compensation_engine import (
    MAX_POPULATION,
    ChainLink,
    CycleTerms,
    FrozenEligibility,
    Guideline,
    active_service_days,
)
from meridian.finance_engine import CostAssignment, FinanceInputError, validate_history
from meridian.scenario_operations import PlanningAssignment

MAX_CHAIN = 512


def freeze_population(
    history: list[PlanningAssignment],
    selected_ids: tuple[str, ...],
    manager_bindings: tuple[tuple[str, str], ...],
    guideline: Guideline,
    terms: CycleTerms,
) -> tuple[FrozenEligibility, ...]:
    """Bindings are trusted server principal/employee edges, never a request owner field.

    The closest ancestor with a trusted manager account owns the frozen roster. An
    employee cannot be their own proposer. Assignment/capability validation and separate
    human approval-route creation must additionally run in the cycle launch transaction.
    """
    if (
        not 1 <= len(selected_ids) <= MAX_POPULATION
        or len(set(selected_ids)) != len(selected_ids)
        or len(dict(manager_bindings)) != len(manager_bindings)
        or len({principal for _, principal in manager_bindings}) != len(manager_bindings)
    ):
        raise FinanceInputError("INVALID_COMPENSATION_POPULATION_OR_BINDINGS")
    if guideline.effective_date > terms.eligibility_date:
        raise FinanceInputError("COMPENSATION_GUIDELINE_NOT_EFFECTIVE")
    validate_history([row.cost for row in history])
    by_person: dict[str, list[CostAssignment]] = defaultdict(list)
    effective: dict[str, PlanningAssignment] = {}
    for row in history:
        by_person[row.cost.employee_id].append(row.cost)
        if row.cost.active_at(terms.eligibility_date):
            effective[row.cost.employee_id] = row
    if any(employee not in by_person for employee in selected_ids):
        raise FinanceInputError("COMPENSATION_EMPLOYEE_NOT_IN_PINNED_SNAPSHOT")
    bindings = dict(manager_bindings)
    result: list[FrozenEligibility] = []
    for employee in sorted(selected_ids):
        person_history = tuple(sorted(by_person[employee], key=lambda row: row.valid_from))
        selected = effective.get(employee)
        reasons: list[str] = []
        chain: list[ChainLink] = []
        proposer: str | None = None
        visited = {employee}
        current = selected
        while current is not None:
            chain.append(
                ChainLink(
                    current.cost.employee_id,
                    current.cost.version_id,
                    current.manager_id,
                    current.cost.source_batch_id,
                )
            )
            manager = current.manager_id
            if manager is None:
                break
            if manager in visited or len(chain) >= MAX_CHAIN:
                raise FinanceInputError("COMPENSATION_APPROVAL_HIERARCHY_CYCLE_OR_LIMIT")
            visited.add(manager)
            if manager not in effective:
                raise FinanceInputError("COMPENSATION_MANAGER_NOT_ACTIVE_IN_PINNED_HIERARCHY")
            if proposer is None and manager in bindings:
                proposer = bindings[manager]
            current = effective[manager]
        service_days = active_service_days(person_history, terms.eligibility_date)
        if selected is None:
            reasons.append("INACTIVE_ON_ELIGIBILITY_DATE")
        if service_days < guideline.minimum_service_days:
            reasons.append("INSUFFICIENT_CALENDAR_SERVICE")
        if selected is not None and int(selected.cost.annual_pay_minor) == 0:
            reasons.append("ZERO_BASE_PAY")
        if proposer is None:
            reasons.append("NO_ASSIGNED_MANAGER")
        result.append(
            FrozenEligibility(
                employee,
                not reasons,
                tuple(reasons),
                selected.cost.org_id if selected else None,
                selected.cost.currency if selected else None,
                selected.cost.annual_pay_minor if selected else None,
                selected.cost.fte if selected else None,
                proposer,
                service_days,
                person_history,
                tuple(chain),
            )
        )
    return tuple(result)
