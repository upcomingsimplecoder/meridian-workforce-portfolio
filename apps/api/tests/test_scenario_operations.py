"""Seven real transformations, fiscal goldens, immutable inputs and scope/graph negatives."""

from dataclasses import replace
from datetime import date
from decimal import Inexact, localcontext
from uuid import UUID, uuid5

import pytest
from meridian.finance_engine import CostAssignment, FinanceInputError, calculate_forecast
from meridian.finance_repository import (
    load_assignments,
    load_fx,
    load_planning_history,
    load_policy,
)
from meridian.finance_seed import FX_BASE_ID, POLICY_ID
from meridian.org_calendar import OrgCalendar, OrgPeriod
from meridian.people_models import EmployeeVersion, OrgVersion
from meridian.people_seed import CURRENT_SNAPSHOT, stable_id
from meridian.scenario_operations import (
    Benefits,
    Departure,
    Hire,
    Merit,
    OperationList,
    PlanningAssignment,
    PlanningRules,
    Promotion,
    Transfer,
    VacancyDelay,
    apply_operations,
    validate_budget,
)
from pydantic import ValidationError
from sqlalchemy import select

SOURCE_BATCH = stable_id("operation-test-source")
ROOT = PlanningAssignment(
    CostAssignment(
        "SYN-000001",
        "root-v1",
        SOURCE_BATCH,
        "D01",
        "Americas",
        date(2027, 1, 1),
        None,
        "ACTIVE",
        "12000000",
        "USD",
        "1.0000",
    ),
    "POS-000001",
    None,
    "L4",
)
LEAF = PlanningAssignment(
    replace(ROOT.cost, employee_id="SYN-000002", version_id="leaf-v1"),
    "POS-000002",
    "SYN-000001",
    "L1",
)
NAMESPACE = UUID(stable_id("operation-tests"))


def operation(cls, label, **kwargs):
    kinds = {
        Hire: "hire",
        Departure: "departure",
        Transfer: "transfer",
        Promotion: "promotion",
        Merit: "merit_assumption",
        Benefits: "benefit_multiplier",
        VacancyDelay: "vacancy_delay",
    }
    return cls(
        **{
            "kind": kinds[cls],
            "id": uuid5(NAMESPACE, label),
            "effective_date": date(2028, 7, 1),
            "reason": "Synthetic planning decision",
        }
        | kwargs
    )


def hire(**kwargs):
    return operation(
        Hire,
        "hire",
        employee_id="SYN-900001",
        position_id="POS-900001",
        org_id="D01",
        manager_id="SYN-000001",
        grade="L1",
        annual_pay_minor="12000000",
        currency="USD",
        fte="1.0000",
        **kwargs,
    )


def apply(ops, *, source=None, **kwargs):
    return apply_operations(
        [ROOT, LEAF] if source is None else source,
        ops,
        **{
            "year": 2028,
            "planning_date": date(2027, 12, 1),
            "scope_orgs": ("D01", "D02"),
            "org_regions": {"D01": "Americas", "D02": "Europe"},
            "rules": PlanningRules(),
            "snapshot_source_batch_id": SOURCE_BATCH,
        }
        | kwargs,
    )


def forecast(db, rows):
    return calculate_forecast(
        [row.cost for row in rows],
        load_policy(db, POLICY_ID),
        load_fx(db, FX_BASE_ID),
        snapshot_id=CURRENT_SNAPSHOT,
        snapshot_sha256="a" * 64,
        year=2028,
        scope_orgs=("D01", "D02"),
    )


def at(rows, employee="SYN-000002", day=date(2028, 7, 1)):
    return next(row for row in rows if row.cost.employee_id == employee and row.cost.active_at(day))


def calendar():
    return OrgCalendar(
        (
            OrgPeriod("D01", "Americas", date(2027, 1, 1), date(2028, 7, 1)),
            OrgPeriod("D01", "Europe", date(2028, 7, 1), None),
            OrgPeriod("D02", "Europe", date(2027, 1, 1), None),
        )
    )


def test_effective_org_regions_split_derived_hire_and_preserve_source():
    ops = [hire(effective_date=date(2028, 2, 29))]
    rows = apply(ops, org_regions=calendar())
    early = at(rows, "SYN-900001", date(2028, 6, 30))
    later = at(rows, "SYN-900001", date(2028, 7, 1))
    assert early.cost.region == "Americas" and later.cost.region == "Europe"
    assert early.cost.valid_to == later.cost.valid_from
    assert early.cost.version_id != later.cost.version_id
    assert at(rows, "SYN-000001").cost.region == "Americas"
    assert rows == apply(ops, org_regions=calendar())


def test_transfer_resolves_destination_region_on_its_date():
    source = [ROOT, replace(LEAF, cost=replace(LEAF.cost, org_id="D02", region="Europe"))]
    transfer = operation(
        Transfer,
        "dated-org-transfer",
        org_id="D01",
        employee_id="SYN-000002",
        manager_id="SYN-000001",
        effective_date=date(2028, 6, 1),
    )
    rows = apply([transfer], source=source, org_regions=calendar())
    assert at(rows, day=date(2028, 6, 1)).cost.region == "Americas"
    assert at(rows, day=date(2028, 7, 1)).cost.region == "Europe"


@pytest.mark.parametrize(
    "periods",
    [
        (OrgPeriod("D01", "Americas", date(2027, 1, 1), date(2028, 7, 1)),),
        (
            OrgPeriod("D01", "Americas", date(2027, 1, 1), None),
            OrgPeriod("D01", "Europe", date(2028, 7, 1), None),
        ),
        (OrgPeriod("D01", "Unknown", date(2027, 1, 1), None),),
    ],
)
def test_org_gap_overlap_and_unknown_region_block_forecast(periods):
    with pytest.raises(FinanceInputError, match="ORGANIZATION_NOT_EFFECTIVE_AT_OPERATION_DATE"):
        apply([], org_regions=OrgCalendar(periods), scope_orgs=("D01",))


def test_hire_and_delay_exact_months_and_source_provenance(db):
    original = hire(effective_date=date(2028, 6, 1))
    hired = apply([original])
    assert forecast(db, hired)["annual"]["base_minor"] == "31000000"
    delayed = apply(
        [
            original,
            operation(
                VacancyDelay,
                "delay",
                effective_date=date(2028, 8, 1),
                hire_operation_id=original.id,
            ),
        ]
    )
    assert forecast(db, delayed)["annual"]["base_minor"] == "29000000"
    assert not any(
        row.cost.active_at(date(2028, 7, 1))
        for row in delayed
        if row.cost.employee_id == original.employee_id
    )
    added = at(delayed, original.employee_id, date(2028, 8, 1))
    assert added.cost.source_batch_id == SOURCE_BATCH
    assert added.cost.version_id == str(original.id)
    assert original.effective_date == date(2028, 6, 1)
    assert apply([original]) == hired  # The delayed branch cannot alter the original.


def test_departure_leap_day_and_exclusive_history(db):
    result = apply(
        [operation(Departure, "exit", employee_id="SYN-000002", effective_date=date(2028, 2, 29))]
    )
    assert at(result, day=date(2028, 2, 28)).cost.valid_to == date(2028, 2, 29)
    exited = [
        row
        for row in result
        if row.cost.employee_id == "SYN-000002" and row.cost.valid_from == date(2028, 2, 29)
    ][0]
    assert exited.cost.status == "EXITED"
    output = forecast(db, result)
    assert output["annual"]["base_minor"] == "13965517"
    assert output["months"][1]["closing_headcount"] == 1


def test_transfer_uses_destination_benefits_and_preserves_pay_currency(db):
    result = apply(
        [
            operation(
                Transfer,
                "transfer",
                employee_id="SYN-000002",
                org_id="D02",
                manager_id="SYN-000001",
            )
        ]
    )
    transferred = at(result)
    assert transferred.cost.org_id == "D02"
    assert transferred.cost.region == "Europe"
    assert transferred.cost.annual_pay_minor == "12000000"
    assert transferred.cost.currency == "USD"
    assert transferred.cost.source_batch_id == SOURCE_BATCH
    output = forecast(db, result)
    assert output["annual"]["base_minor"] == "24000000"
    assert output["annual"]["benefits_minor"] == "6300000"
    assert [org["closing_headcount"] for org in output["months"][6]["departments"]] == [1, 1]
    assert at(result, day=date(2028, 6, 30)).cost.org_id == "D01"


def test_promotion_splits_source_without_mutation_and_exact_delta(db):
    op = operation(
        Promotion, "promotion", employee_id="SYN-000002", grade="L2", annual_pay_minor="14400000"
    )
    source = [ROOT, LEAF]
    promoted = apply([op], source=source)
    assert at(promoted).grade == "L2"
    assert at(promoted, day=date(2028, 6, 30)).grade == "L1"
    assert LEAF.cost.valid_to is None
    assert LEAF.cost.annual_pay_minor == "12000000"
    assert forecast(db, promoted)["annual"]["base_minor"] == "25200000"
    assert apply([op], source=list(reversed(source))) == promoted


def test_merit_active_population_original_minor_rounding_and_future_hires(db):
    merit = operation(Merit, "merit", org_id="D01", rate="0.10")
    hired = hire(effective_date=date(2028, 8, 1))
    result = apply([merit, hired])
    assert at(result).cost.annual_pay_minor == "13200000"
    assert at(result, hired.employee_id, date(2028, 8, 1)).cost.annual_pay_minor == "12000000"
    assert forecast(db, result)["annual"]["base_minor"] == "30200000"
    pennies = replace(LEAF, cost=replace(LEAF.cost, annual_pay_minor="5"))
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        rounded = apply([merit], source=[ROOT, pennies])
    # 5 original minor units * 1.10 = 5.5 -> 6 (even).
    assert at(rounded).cost.annual_pay_minor == "6"


def test_benefits_ongoing_assumption_includes_future_hires_and_fingerprints(db):
    benefits = operation(Benefits, "benefits", org_id="D01", multiplier="2")
    result = apply([benefits, hire(effective_date=date(2028, 8, 1))])
    assert at(result).cost.benefit_multiplier == "2"
    assert at(result, "SYN-900001", date(2028, 8, 1)).cost.benefit_multiplier == "2"
    assert forecast(db, result)["annual"]["benefits_minor"] == "20000000"
    assert (
        forecast(db, result)["inputs"]["assignment_sha256"]
        != forecast(db, [ROOT, LEAF])["inputs"]["assignment_sha256"]
    )
    assert at(result, day=date(2028, 6, 30)).cost.benefit_multiplier is None


def test_multiple_benefit_changes_split_exactly_and_transfer_resets_to_region(db):
    first = operation(
        Benefits, "benefits1", org_id="D01", multiplier="2", effective_date=date(2028, 5, 1)
    )
    second = operation(
        Benefits, "benefits2", org_id="D01", multiplier="1.5", effective_date=date(2028, 6, 1)
    )
    transfer = operation(
        Transfer, "transfer", employee_id="SYN-000002", org_id="D02", manager_id="SYN-000001"
    )
    result = apply([first, second, transfer])
    assert at(result, day=date(2028, 5, 31)).cost.benefit_multiplier == "2"
    assert at(result, day=date(2028, 6, 30)).cost.benefit_multiplier == "1.5"
    assert at(result).cost.benefit_multiplier is None
    assert forecast(db, result)["months"][6]["benefits_minor"] == "800000"


def test_undo_as_new_operation_list_and_independent_branches(db):
    op = operation(
        Promotion, "promote", employee_id="SYN-000002", grade="L2", annual_pay_minor="14400000"
    )
    branch_a = apply([op])
    branch_b = apply([hire()])
    old = forecast(db, branch_a)
    undone = apply([])
    assert forecast(db, branch_a) == old
    assert forecast(db, branch_b)["annual"]["base_minor"] == "30000000"
    assert forecast(db, undone)["annual"]["base_minor"] == "24000000"


def test_whole_5k_merit_and_transfer_from_real_snapshot(db):
    snapshot, costs = load_assignments(db, CURRENT_SNAPSHOT)
    metadata = {
        row.id: row
        for row in db.scalars(
            select(EmployeeVersion).where(EmployeeVersion.snapshot_id == snapshot.id)
        )
    }
    source = [
        PlanningAssignment(
            cost,
            metadata[cost.version_id].position_id,
            metadata[cost.version_id].manager_id,
            metadata[cost.version_id].grade,
        )
        for cost in costs
    ]
    loaded_snapshot, loaded_source = load_planning_history(db, CURRENT_SNAPSHOT)
    assert loaded_snapshot.id == snapshot.id
    assert set(loaded_source) == set(source)
    regions = dict(
        db.execute(
            select(OrgVersion.org_id, OrgVersion.region).where(
                OrgVersion.snapshot_id == snapshot.id
            )
        ).all()
    )
    merit = operation(Merit, "5k-merit", org_id="D01", rate="0.03")
    result = apply_operations(
        source,
        [merit],
        year=2028,
        planning_date=date(2027, 12, 1),
        scope_orgs=tuple(f"D{i:02d}" for i in range(1, 13)),
        org_regions=regions,
        rules=PlanningRules(),
        snapshot_source_batch_id=snapshot.source_batch_id,
    )
    assert len(result) == len(source) + 415
    assert len({row.cost.employee_id for row in result}) == 5000
    originals = {
        row.cost.employee_id: row.cost.annual_pay_minor
        for row in source
        if row.cost.active_at(date(2028, 7, 1))
    }
    for row in result:
        if not row.cost.active_at(date(2028, 7, 1)):
            continue
        if row.cost.org_id == "D01":
            # Independent integer half-even at the original salary boundary.
            quotient, remainder = divmod(int(originals[row.cost.employee_id]) * 103, 100)
            expected = quotient + int(remainder > 50 or (remainder == 50 and quotient % 2))
            assert row.cost.annual_pay_minor == str(expected)
        else:
            assert row.cost.annual_pay_minor == originals[row.cost.employee_id]


@pytest.mark.security
@pytest.mark.parametrize(
    "override,code",
    [
        ({"effective_date": date(2028, 1, 1)}, "HIRE_LEAD_TIME_NOT_MET"),
        ({"employee_id": "SYN-000002"}, "HIRE_IDENTITY_ALREADY_EXISTS"),
        ({"position_id": "POS-000002"}, "HIRE_POSITION_ALREADY_EXISTS"),
        ({"manager_id": "SYN-999999"}, "OPERATION_REQUIRES_ACTIVE_EMPLOYEE"),
        ({"org_id": "D03"}, "OPERATION_CROSSES_PLANNING_SCOPE"),
        ({"fte": "0"}, "DECIMAL_OUT_OF_RANGE"),
    ],
)
def test_invalid_hires_blocked(override, code):
    op = hire().model_copy(update=override)
    with pytest.raises(FinanceInputError, match=code):
        apply([op], planning_date=date(2027, 12, 31))


@pytest.mark.security
@pytest.mark.parametrize(
    "cls,fields,code",
    [
        (
            Promotion,
            {"employee_id": "SYN-000002", "grade": "L1", "annual_pay_minor": "14400000"},
            "PROMOTION_REQUIRES_HIGHER_GRADE",
        ),
        (
            Promotion,
            {"employee_id": "SYN-000002", "grade": "L2", "annual_pay_minor": "11999999"},
            "PROMOTION_CANNOT_REDUCE_SALARY",
        ),
        (Departure, {"employee_id": "SYN-000001"}, "PLANNING_MANAGER_NOT_ACTIVE"),
        (
            Transfer,
            {"employee_id": "SYN-000001", "org_id": "D02", "manager_id": "SYN-000002"},
            "PLANNING_MANAGER_CYCLE",
        ),
        (
            Transfer,
            {"employee_id": "SYN-000002", "org_id": "D02", "manager_id": "SYN-000002"},
            "PLANNING_MANAGER_CYCLE",
        ),
        (
            Transfer,
            {"employee_id": "SYN-000002", "org_id": "D03", "manager_id": None},
            "OPERATION_CROSSES_PLANNING_SCOPE",
        ),
        (Merit, {"org_id": "D03", "rate": "0.03"}, "OPERATION_CROSSES_PLANNING_SCOPE"),
        (Merit, {"org_id": "D01", "rate": "0.26"}, "DECIMAL_OUT_OF_RANGE"),
        (Benefits, {"org_id": "D03", "multiplier": "2"}, "OPERATION_CROSSES_PLANNING_SCOPE"),
        (Benefits, {"org_id": "D01", "multiplier": "3.1"}, "DECIMAL_OUT_OF_RANGE"),
    ],
)
def test_invalid_changes_and_graphs_blocked(cls, fields, code):
    with pytest.raises(FinanceInputError, match=code):
        apply([operation(cls, "invalid", **fields)])


@pytest.mark.security
def test_scope_cannot_modify_future_outside_assignment_or_choose_outside_manager():
    prior = replace(LEAF, cost=replace(LEAF.cost, valid_to=date(2028, 8, 1)))
    future = replace(
        LEAF,
        cost=replace(
            LEAF.cost,
            valid_from=date(2028, 8, 1),
            version_id="outside-v2",
            org_id="D02",
            region="Europe",
        ),
    )
    outside_manager = replace(
        ROOT,
        cost=replace(ROOT.cost, employee_id="SYN-000003", org_id="D02"),
        position_id="POS-000003",
    )
    with pytest.raises(FinanceInputError, match="OPERATION_CROSSES_PLANNING_SCOPE"):
        apply(
            [operation(Departure, "exit", employee_id="SYN-000002")],
            source=[ROOT, prior, future],
            scope_orgs=("D01",),
        )
    with pytest.raises(FinanceInputError, match="MANAGER_OUTSIDE_PLANNING_SCOPE"):
        apply(
            [hire().model_copy(update={"manager_id": "SYN-000003"})],
            source=[ROOT, LEAF, outside_manager],
            scope_orgs=("D01",),
        )


@pytest.mark.security
def test_chronology_duplicate_ids_work_budget_and_delays():
    initial = hire()
    cases = [
        ([initial, initial], "DUPLICATE_ID"),
        (
            [
                operation(
                    Merit, "late", org_id="D01", rate="0.03", effective_date=date(2028, 8, 1)
                ),
                initial,
            ],
            "CHRONOLOGICAL",
        ),
        (
            [
                operation(
                    Departure, "outside", employee_id="SYN-000002", effective_date=date(2029, 1, 1)
                )
            ],
            "OUTSIDE_PLANNING_PERIOD",
        ),
        (
            [operation(VacancyDelay, "delay", hire_operation_id=initial.id)],
            "DELAY_REQUIRES_PRIOR_HIRE",
        ),
        (
            [initial, operation(VacancyDelay, "delay", hire_operation_id=initial.id)],
            "VACANCY_DELAY_MUST_MOVE_LATER",
        ),
        (
            [initial.model_copy(update={"id": uuid5(NAMESPACE, str(i))}) for i in range(201)],
            "INVALID_OPERATION_COUNT",
        ),
    ]
    for ops, code in cases:
        with pytest.raises(FinanceInputError, match=code):
            apply(ops)


@pytest.mark.security
@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "hr"),
        ("role", "hr"),
        ("annual_pay_minor", 12000000),
        ("currency", "XXX"),
        ("effective_date", "2028-07-01T00:00:00Z"),
        ("effective_date", 1846022400),
        ("reason", "   "),
        ("fte", 0.5),
    ],
)
def test_operation_contract_rejects_overposting_floats_and_ambiguous_dates(field, value):
    data = hire().model_dump(mode="json") | {field: value}
    with pytest.raises(ValidationError):
        OperationList.validate_python([data])


@pytest.mark.security
def test_budget_cap_validates_exact_minor_units(db):
    output = forecast(db, [ROOT, LEAF])
    assert output["annual"]["total_minor"] == "33600000"
    validate_budget(output, "33600000")
    with pytest.raises(FinanceInputError, match="FORECAST_EXCEEDS_PINNED_BUDGET"):
        validate_budget(output, "33599999")
    with pytest.raises(FinanceInputError, match="INVALID_DECIMAL_STRING"):
        validate_budget(output, 33600000)


@pytest.mark.security
def test_operation_values_are_frozen_and_rules_require_integer_days():
    op = hire()
    with pytest.raises(ValidationError):
        op.reason = "changed after fingerprint"
    for days in (True, 14.5, -1, 366):
        with pytest.raises(FinanceInputError, match="INVALID_PLANNING_PERIOD_OR_RULES"):
            apply([], rules=PlanningRules(days))


def test_merit_preserves_later_source_salary_changes():
    earlier = replace(LEAF, cost=replace(LEAF.cost, valid_to=date(2028, 8, 1)))
    later = replace(
        LEAF,
        cost=replace(
            LEAF.cost,
            valid_from=date(2028, 8, 1),
            version_id="late-source-correction",
            annual_pay_minor="18000000",
        ),
    )
    result = apply(
        [operation(Merit, "merit", org_id="D01", rate="0.10")], source=[ROOT, earlier, later]
    )
    assert at(result).cost.annual_pay_minor == "13200000"
    assert at(result, day=date(2028, 8, 1)).cost.annual_pay_minor == "19800000"
    assert later.cost.annual_pay_minor == "18000000"


def test_atomic_reparenting_then_manager_departure_at_same_date():
    second_root = replace(
        ROOT,
        cost=replace(
            ROOT.cost, employee_id="SYN-000003", version_id="root3", org_id="D02", region="Europe"
        ),
        position_id="POS-000003",
    )
    transfer = operation(
        Transfer, "reparent", employee_id="SYN-000002", org_id="D01", manager_id="SYN-000003"
    )
    departure = operation(Departure, "manager-exit", employee_id="SYN-000001")
    result = apply([transfer, departure], source=[ROOT, LEAF, second_root])
    assert at(result).manager_id == "SYN-000003"
    assert not any(
        row.cost.active_at(date(2028, 7, 1))
        for row in result
        if row.cost.employee_id == "SYN-000001"
    )
    assert at(result, day=date(2028, 6, 30)).manager_id == "SYN-000001"


def test_half_time_hire_operation_on_leap_day_matches_golden(db):
    result = apply([hire(effective_date=date(2028, 2, 29)).model_copy(update={"fte": "0.5000"})])
    output = forecast(db, result)
    # Two existing full-time workers + the one-day half-time hire.
    assert output["months"][1]["base_minor"] == "2017241"
    assert output["months"][1]["total_minor"] == "2824137"
    assert output["months"][1]["closing_fte"] == "2.5000"


@pytest.mark.security
def test_merit_overflow_rejected_without_truncation_or_source_mutation():
    large = replace(LEAF, cost=replace(LEAF.cost, annual_pay_minor="99999999999999999999"))
    with pytest.raises(FinanceInputError, match="DECIMAL_OUT_OF_RANGE"):
        apply([operation(Merit, "merit", org_id="D01", rate="0.10")], source=[ROOT, large])
    assert large.cost.annual_pay_minor == "99999999999999999999"
