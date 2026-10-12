"""Independent fiscal oracles, frozen hierarchy and invalid compensation input cases."""

import calendar
from dataclasses import replace
from datetime import date
from decimal import Inexact, localcontext
from fractions import Fraction

import pytest
from meridian.compensation_eligibility import freeze_population
from meridian.compensation_engine import (
    MAX_MINOR,
    CycleTerms,
    Guideline,
    Pool,
    calculate_award,
    evaluate_pool,
)
from meridian.compensation_seed import DEMO_GUIDELINE
from meridian.finance_engine import CostAssignment, FinanceInputError
from meridian.finance_seed import fixture_fx
from meridian.policy import canonical_hash
from meridian.scenario_operations import PlanningAssignment
from pydantic import ValidationError

TERMS = CycleTerms(
    eligibility_date=date(2028, 3, 31),
    merit_effective_date=date(2028, 5, 1),
    bonus_service_start=date(2028, 1, 1),
    bonus_service_end=date(2029, 1, 1),
    bonus_payment_date=date(2029, 1, 15),
)
ROOT = PlanningAssignment(
    CostAssignment(
        "SYN-000001",
        "root-version",
        "source-batch",
        "D01",
        "Americas",
        date(2027, 1, 1),
        None,
        "ACTIVE",
        "12000000",
        "USD",
        "1",
    ),
    "POS-000001",
    None,
    "L4",
)
LEAF = PlanningAssignment(
    replace(ROOT.cost, employee_id="SYN-000002", version_id="leaf-version"),
    "POS-000002",
    "SYN-000001",
    "L2",
)


def eligible(*, rows=None, terms=TERMS, guideline=DEMO_GUIDELINE):
    return freeze_population(
        [ROOT, LEAF] if rows is None else rows,
        ("SYN-000002",),
        (("SYN-000001", "manager-a"),),
        guideline,
        terms,
    )[0]


def award(*, roster=None, guideline=DEMO_GUIDELINE, terms=TERMS, fx=None, **kwargs):
    return calculate_award(
        eligible(terms=terms, guideline=guideline) if roster is None else roster,
        guideline,
        terms,
        fixture_fx() if fx is None else fx,
        **{"merit_minor": "120000", "bonus_minor": "1000000"} | kwargs,
    )


def half_even(value):
    # Fraction is intentionally independent of the production Decimal implementation.
    whole, remainder = divmod(value.numerator, value.denominator)
    return whole + int(
        2 * remainder > value.denominator or (2 * remainder == value.denominator and whole % 2)
    )


def test_full_time_merit_annualized_once_and_bonus_one_time():
    result = award()
    assert result["annualized_merit_minor"] == "120000"
    assert (
        result["merit_first_year_minor"] == "80000"
    )  # Eight monthly costs, not a full-year budget.
    assert result["bonus_payable_minor"] == "1000000"
    assert result["pool_charge_minor"] == "1120000"
    assert result["reporting_pool_charge_minor"] == "1120000"
    assert result["bonus_service_fte_days"] == "366"
    assert result["bonus_service_calendar_days"] == 366
    assert [month["merit_cost_minor"] for month in result["merit_months"]] == ["0"] * 4 + [
        "10000"
    ] * 8
    assert not result["blockers"] and not result["exceptions"] and not result["warnings"]
    assert result["sha256"] == canonical_hash({k: v for k, v in result.items() if k != "sha256"})


def test_leap_day_hire_half_fte_exact_service_proration_golden():
    leaf = replace(LEAF, cost=replace(LEAF.cost, valid_from=date(2028, 2, 29), fte="0.5000"))
    roster = eligible(rows=[ROOT, leaf])
    assert roster.service_days == 32  # Feb29 + all31 March days, eligibility date inclusive.
    result = award(roster=roster, bonus_minor="100000")
    assert result["annualized_merit_minor"] == "60000"
    assert result["merit_first_year_minor"] == "40000"
    assert result["bonus_service_fte_days"] == "153.5000"  # 307 active days × .5, exactly once.
    assert result["bonus_payable_minor"] == "41940"
    assert result["pool_charge_minor"] == "101940"
    assert half_even(Fraction(100000 * 307, 2 * 366)) == 41940
    assert roster.history[0].annual_pay_minor == "12000000"


@pytest.mark.parametrize("month,day", [(2, 28), (2, 29), (4, 30), (5, 31), (10, 15)])
@pytest.mark.parametrize("fte", ["1", "0.8", "0.5000"])
def test_merit_month_end_proration_against_independent_fraction_oracle(month, day, fte):
    terms = TERMS.model_copy(
        update={
            "merit_effective_date": date(2028, month, day),
            "eligibility_date": date(2028, 1, 31),
        }
    )
    roster = eligible(rows=[ROOT, replace(LEAF, cost=replace(LEAF.cost, fte=fte))], terms=terms)
    result = award(roster=roster, terms=terms, merit_minor="120001", bonus_minor="0")
    expected = []
    for current in range(1, 13):
        days = calendar.monthrange(2028, current)[1]
        active_days = 0 if current < month else days - day + 1 if current == month else days
        expected.append(half_even(Fraction(120001 * active_days, 12 * days) * Fraction(fte)))
    assert [int(row["merit_cost_minor"]) for row in result["merit_months"]] == expected
    assert int(result["merit_first_year_minor"]) == sum(expected)
    assert int(result["annualized_merit_minor"]) == half_even(Fraction(120001) * Fraction(fte))


@pytest.mark.parametrize("target,expected", [(1, 0), (3, 2), (5, 2), (7, 4)])
def test_original_minor_half_even_ties_and_global_context_independence(target, expected):
    roster = eligible(rows=[ROOT, replace(LEAF, cost=replace(LEAF.cost, fte="0.5"))])
    normal = award(roster=roster, merit_minor=str(target), bonus_minor=str(target))
    assert int(normal["annualized_merit_minor"]) == expected
    assert int(normal["bonus_payable_minor"]) == expected
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert award(roster=roster, merit_minor=str(target), bonus_minor=str(target)) == normal


def test_segmented_service_and_monthly_merit_do_not_double_round_or_double_fte():
    split = date(2028, 5, 17)
    leaf = replace(LEAF, cost=replace(LEAF.cost, fte="0.8"))
    before = replace(leaf, cost=replace(leaf.cost, valid_to=split))
    after = replace(leaf, cost=replace(leaf.cost, valid_from=split, version_id="after-correction"))
    whole = award(roster=eligible(rows=[ROOT, leaf]), merit_minor="120007", bonus_minor="100007")
    segmented = award(
        roster=eligible(rows=[ROOT, before, after]), merit_minor="120007", bonus_minor="100007"
    )
    for field in (
        "annualized_merit_minor",
        "merit_first_year_minor",
        "merit_months",
        "bonus_payable_minor",
        "pool_charge_minor",
        "bonus_service_fte_days",
    ):
        assert whole[field] == segmented[field]
    assert whole["inputs"]["eligibility_sha256"] != segmented["inputs"]["eligibility_sha256"]


def test_frozen_future_fte_change_and_departure_change_first_year_not_effective_annualization():
    january = TERMS.model_copy(
        update={"eligibility_date": date(2028, 1, 31), "merit_effective_date": date(2028, 2, 1)}
    )
    first = replace(LEAF, cost=replace(LEAF.cost, valid_to=date(2028, 7, 1)))
    second = replace(
        LEAF,
        cost=replace(
            LEAF.cost,
            valid_from=date(2028, 7, 1),
            valid_to=date(2028, 10, 1),
            fte="0.5",
            version_id="part-time",
        ),
    )
    exit_row = replace(
        LEAF,
        cost=replace(LEAF.cost, valid_from=date(2028, 10, 1), status="EXITED", version_id="exit"),
    )
    result = award(
        roster=eligible(rows=[ROOT, first, second, exit_row], terms=january),
        terms=january,
        merit_minor="120000",
        bonus_minor="120000",
    )
    assert result["annualized_merit_minor"] == "120000"
    assert result["merit_first_year_minor"] == "65000"  # Feb–Jun5×10k + Jul–Sep3×5k.
    assert result["bonus_service_fte_days"] == "228.0"  # 182 full +92×.5; exited days excluded.
    assert int(result["bonus_payable_minor"]) == half_even(Fraction(120000 * 228, 366))


@pytest.mark.parametrize(
    "currency,base,merit,bonus",
    [
        ("JPY", "12000000", "120000", "1000000"),
        ("USD", "12000000", "120000", "1000000"),
        ("KWD", "36000000", "360000", "3000000"),
        ("EUR", "12000000", "120000", "1000000"),
    ],
)
def test_original_currency_exponents_and_pinned_directional_fx(currency, base, merit, bonus):
    exponents = {"JPY": 0, "USD": 2, "KWD": 3, "EUR": 2}
    roster = eligible(
        rows=[
            ROOT,
            replace(LEAF, cost=replace(LEAF.cost, currency=currency, annual_pay_minor=base)),
        ]
    )
    fx = fixture_fx()
    result = award(roster=roster, merit_minor=merit, bonus_minor=bonus, fx=fx)
    expected = half_even(
        Fraction((int(merit) + int(bonus)) * 100, 10 ** exponents[currency])
        * Fraction(dict(fx.rates)[currency])
    )
    assert result["currency_exponent"] == exponents[currency]
    assert int(result["reporting_pool_charge_minor"]) == expected
    if currency == "EUR":
        changed = award(
            roster=roster, merit_minor=merit, bonus_minor=bonus, fx=fixture_fx(shock=True)
        )
        assert changed["pool_charge_minor"] == result["pool_charge_minor"]
        assert changed["reporting_pool_charge_minor"] == "1344000"
        assert result["reporting_pool_charge_minor"] == "1232000"
        assert award(roster=roster, merit_minor=merit, bonus_minor=bonus, fx=fx) == result


@pytest.mark.parametrize(
    "merit,bonus,blockers,exceptions,warnings",
    [
        ("0", "0", [], [], ["ZERO_AWARD"]),
        ("480000", "1200000", [], [], []),
        ("480001", "1200001", [], ["MERIT_GUIDELINE_EXCEPTION", "BONUS_GUIDELINE_EXCEPTION"], []),
        ("2400000", "6000000", [], ["MERIT_GUIDELINE_EXCEPTION", "BONUS_GUIDELINE_EXCEPTION"], []),
        ("2400001", "6000001", ["MERIT_HARD_CAP_EXCEEDED", "BONUS_HARD_CAP_EXCEEDED"], [], []),
    ],
)
def test_warnings_exceptions_and_hard_blockers_are_distinct(
    merit, bonus, blockers, exceptions, warnings
):
    result = award(merit_minor=merit, bonus_minor=bonus)
    assert result["blockers"] == blockers
    assert result["exceptions"] == exceptions
    assert result["warnings"] == warnings


def test_twenty_digit_amounts_remain_exact_and_overflow_is_a_blocker():
    roster = eligible(
        rows=[ROOT, replace(LEAF, cost=replace(LEAF.cost, annual_pay_minor=str(MAX_MINOR)))]
    )
    value = "10000000000000000001"
    result = award(roster=roster, merit_minor=value, bonus_minor="0")
    assert result["annualized_merit_minor"] == value
    assert result["pool_charge_minor"] == value
    assert result["blockers"] == ["RESULTING_BASE_PAY_OVERFLOW"]


@pytest.mark.parametrize(
    "value", [1, 1.0, "-1", "+1", "01", "1.0", "1e3", "", "100000000000000000000", " 10", "NaN"]
)
def test_malformed_money_is_rejected_without_coercion(value):
    with pytest.raises(FinanceInputError, match="INVALID_COMPENSATION_MINOR_UNITS"):
        award(merit_minor=value)


@pytest.mark.parametrize(
    "override",
    [
        {"minimum_service_days": -1},
        {"minimum_service_days": True},
        {"minimum_service_days": 367},
        {"merit_guideline_rate": 0.04},
        {"merit_guideline_rate": "0.21"},
        {"bonus_hard_cap_rate": "1.01"},
        {"bonus_hard_cap_rate": "-1"},
        {"timezone": "UTC"},
        {"rounding": "ROUND_HALF_UP"},
        {"recommend_workers": True},
    ],
)
def test_invalid_guideline_definitions_rejected(override):
    with pytest.raises((ValidationError, FinanceInputError)):
        Guideline(**{"effective_date": date(2026, 1, 1)} | override)


@pytest.mark.parametrize(
    "override",
    [
        {"bonus_service_end": date(2028, 1, 1)},
        {"bonus_service_end": date(2030, 1, 1)},
        {"bonus_payment_date": date(2028, 12, 31)},
        {"eligibility_date": date(2028, 5, 2)},
        {"eligibility_date": date(2027, 12, 31)},
        {"merit_effective_date": date(2200, 1, 1)},
    ],
)
def test_invalid_cycle_chronology_rejected(override):
    with pytest.raises((ValidationError, FinanceInputError)):
        CycleTerms(**{field: getattr(TERMS, field) for field in CycleTerms.model_fields} | override)


def test_eligibility_is_transparent_effective_dated_and_proposer_is_trusted_ancestor():
    roster = eligible()
    assert roster.eligible and not roster.reason_codes and roster.proposer_id == "manager-a"
    assert [link.employee_id for link in roster.chain] == ["SYN-000002", "SYN-000001"]
    assert roster.document()["chain"][0]["version_id"] == "leaf-version"
    root = freeze_population(
        [ROOT, LEAF], ("SYN-000001",), (("SYN-000001", "manager-a"),), DEMO_GUIDELINE, TERMS
    )[0]
    assert not root.eligible and root.proposer_id is None  # Never their own compensation proposer.
    assert root.reason_codes == ("NO_ASSIGNED_MANAGER",)


def test_minimum_service_uses_real_days_before_as_of_and_not_projected_or_fte_days():
    on_threshold = replace(LEAF, cost=replace(LEAF.cost, valid_from=date(2028, 3, 2), fte="0.5"))
    assert eligible(rows=[ROOT, on_threshold]).service_days == 30
    assert eligible(rows=[ROOT, on_threshold]).eligible
    new = replace(on_threshold, cost=replace(on_threshold.cost, valid_from=date(2028, 3, 3)))
    roster = eligible(rows=[ROOT, new])
    assert roster.service_days == 29 and not roster.eligible
    assert roster.reason_codes == ("INSUFFICIENT_CALENDAR_SERVICE",)
    with pytest.raises(FinanceInputError, match="INELIGIBLE"):
        award(roster=roster)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "EXITED"}, "INACTIVE_ON_ELIGIBILITY_DATE"),
        ({"valid_from": date(2028, 4, 1)}, "INACTIVE_ON_ELIGIBILITY_DATE"),
        ({"annual_pay_minor": "0"}, "ZERO_BASE_PAY"),
    ],
)
def test_ineligible_workers_are_not_silently_omitted(change, reason):
    roster = eligible(rows=[ROOT, replace(LEAF, cost=replace(LEAF.cost, **change))])
    assert not roster.eligible and reason in roster.reason_codes
    assert roster.employee_id == "SYN-000002"


def test_departure_before_effective_merit_is_a_blocker_but_bonus_remains_prorated():
    leaf = replace(LEAF, cost=replace(LEAF.cost, valid_to=date(2028, 4, 1)))
    exit_row = replace(
        LEAF,
        cost=replace(LEAF.cost, valid_from=date(2028, 4, 1), status="EXITED", version_id="exited"),
    )
    roster = eligible(rows=[ROOT, leaf, exit_row])
    result = award(roster=roster)
    assert result["blockers"] == ["INACTIVE_ON_MERIT_EFFECTIVE_DATE"]
    assert result["annualized_merit_minor"] == "0"
    assert result["merit_first_year_minor"] == "0"
    assert int(result["bonus_payable_minor"]) == half_even(Fraction(1000000 * 91, 366))
    assert not award(roster=roster, merit_minor="0")["blockers"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("overlap", "OVERLAPPING_COST_ASSIGNMENTS"),
        ("cycle", "HIERARCHY_CYCLE"),
        ("missing_manager", "MANAGER_NOT_ACTIVE"),
    ],
)
def test_invalid_source_hierarchy_fails_closed(mutation, code):
    rows = [ROOT, LEAF]
    if mutation == "overlap":
        rows.append(replace(LEAF, cost=replace(LEAF.cost, version_id="duplicate")))
    elif mutation == "cycle":
        rows[0] = replace(ROOT, manager_id="SYN-000002")
    else:
        rows[0] = replace(ROOT, cost=replace(ROOT.cost, status="EXITED"))
    with pytest.raises(FinanceInputError, match=code):
        eligible(rows=rows)


@pytest.mark.parametrize(
    "ids,bindings,code",
    [
        ((), (("SYN-000001", "manager-a"),), "POPULATION"),
        (("SYN-000002", "SYN-000002"), (("SYN-000001", "manager-a"),), "POPULATION"),
        (("SYN-999999",), (("SYN-000001", "manager-a"),), "NOT_IN_PINNED_SNAPSHOT"),
        (("SYN-000002",), (("SYN-000001", "a"), ("SYN-000001", "b")), "BINDINGS"),
        (("SYN-000002",), (("SYN-000001", "a"), ("SYN-000002", "a")), "BINDINGS"),
    ],
)
def test_population_and_principal_binding_ambiguities_fail_closed(ids, bindings, code):
    with pytest.raises(FinanceInputError, match=code):
        freeze_population([ROOT, LEAF], ids, bindings, DEMO_GUIDELINE, TERMS)


def test_closest_trusted_manager_and_full_ancestor_chain_are_frozen():
    middle = replace(LEAF, cost=replace(LEAF.cost, employee_id="SYN-000003", version_id="middle"))
    leaf = replace(LEAF, manager_id="SYN-000003")
    frozen = freeze_population(
        [ROOT, middle, leaf],
        ("SYN-000002",),
        (("SYN-000001", "manager-a"), ("SYN-000003", "manager-b")),
        DEMO_GUIDELINE,
        TERMS,
    )[0]
    assert frozen.proposer_id == "manager-b"
    assert [link.employee_id for link in frozen.chain] == ["SYN-000002", "SYN-000003", "SYN-000001"]
    altered = replace(leaf, manager_id="SYN-000001")
    changed = freeze_population(
        [ROOT, middle, altered],
        ("SYN-000002",),
        (("SYN-000001", "manager-a"), ("SYN-000003", "manager-b")),
        DEMO_GUIDELINE,
        TERMS,
    )[0]
    assert changed.proposer_id == "manager-a"
    assert frozen.proposer_id == "manager-b" and changed.sha256 != frozen.sha256


def test_missing_fx_unsupported_currency_change_and_not_effective_guideline_are_explicit_errors():
    missing = replace(fixture_fx(), rates=(("USD", "1"),))
    eur = eligible(rows=[ROOT, replace(LEAF, cost=replace(LEAF.cost, currency="EUR"))])
    with pytest.raises(FinanceInputError, match="MISSING_PINNED_FX_RATE"):
        award(roster=eur, fx=missing)
    with pytest.raises(FinanceInputError, match="FX_DOES_NOT_COVER"):
        award(fx=replace(fixture_fx(), effective_end=date(2029, 1, 1)))
    before = replace(LEAF, cost=replace(LEAF.cost, valid_to=date(2028, 6, 1)))
    after = replace(
        LEAF, cost=replace(LEAF.cost, valid_from=date(2028, 6, 1), currency="EUR", version_id="eur")
    )
    with pytest.raises(FinanceInputError, match="CURRENCY_CHANGE"):
        award(roster=eligible(rows=[ROOT, before, after]))
    with pytest.raises(FinanceInputError, match="NOT_EFFECTIVE"):
        award(guideline=Guideline(effective_date=date(2028, 4, 1)))


@pytest.mark.parametrize(
    "budget,holdback,blockers,exceptions,warnings",
    [
        ("2000000", "100000", [], [], []),
        ("1200000", "100000", [], ["POOL_HOLDBACK_RELEASE"], []),
        ("1120000", "100000", [], ["POOL_HOLDBACK_RELEASE"], ["POOL_AT_BUDGET"]),
        ("1119999", "100000", ["POOL_BUDGET_EXCEEDED"], [], []),
    ],
)
def test_pool_budget_reservations_distinguish_holdback_exception_and_absolute_block(
    budget, holdback, blockers, exceptions, warnings
):
    result = evaluate_pool(Pool("D01", "USD", "CC-001", budget, holdback), [award()])
    assert result["utilized_minor"] == "1120000"
    assert result["blockers"] == blockers
    assert result["exceptions"] == exceptions
    assert result["warnings"] == warnings
    assert int(result["remaining_total_minor"]) + int(result["utilized_minor"]) == int(budget)
    assert result["award_count"] == 1


def test_pool_no_float_rounding_cross_currency_duplicate_or_forged_award():
    pool = Pool("D01", "USD", "CC-001", "2000000", "0")
    for awards, code in [
        ([award(), award()], "DUPLICATE"),
        ([award() | {"currency": "EUR"}], "POOL_MISMATCH"),
        ([award() | {"pool_charge_minor": "1"}], "FINGERPRINT_MISMATCH"),
    ]:
        with pytest.raises(FinanceInputError, match=code):
            evaluate_pool(pool, awards)
    for invalid in [
        replace(pool, holdback_minor="2000001"),
        replace(pool, currency="XXX"),
        replace(pool, budget_minor="1.0"),
        replace(pool, cost_center="=formula"),
    ]:
        with pytest.raises(FinanceInputError):
            evaluate_pool(invalid, [])
    assert evaluate_pool(pool, [])["utilized_minor"] == "0"


def test_guideline_terms_and_eligibility_are_frozen_and_canonical_order_independent():
    with pytest.raises(ValidationError):
        TERMS.merit_effective_date = date(2028, 6, 1)
    with pytest.raises(ValidationError):
        DEMO_GUIDELINE.merit_guideline_rate = "0.9"
    first = eligible()
    assert eligible(rows=[LEAF, ROOT]).document() == first.document()
    assert award(roster=first) == award(roster=first)


def test_known_late_salary_correction_overflow_is_not_hidden_by_eligibility_reference_pay():
    early = replace(LEAF, cost=replace(LEAF.cost, valid_to=date(2028, 7, 1)))
    corrected = replace(
        LEAF,
        cost=replace(
            LEAF.cost,
            valid_from=date(2028, 7, 1),
            version_id="late-pay",
            annual_pay_minor=str(MAX_MINOR),
        ),
    )
    roster = eligible(rows=[ROOT, early, corrected])
    result = award(roster=roster)
    assert result["merit_reference_base_pay_minor"] == "12000000"
    assert result["effective_base_pay_minor"] == "12000000"
    assert result["resulting_base_pay_minor"] == "12120000"
    assert result["blockers"] == ["RESULTING_BASE_PAY_OVERFLOW"]
    # The salary correction affects validity, but an increment remains the same increment.
    assert result["annualized_merit_minor"] == "120000"
    assert result["merit_first_year_minor"] == "80000"


def test_extreme_invalid_award_still_reports_caps_and_pool_budget_blockers():
    result = award(merit_minor=str(MAX_MINOR), bonus_minor=str(MAX_MINOR))
    assert {
        "MERIT_HARD_CAP_EXCEEDED",
        "BONUS_HARD_CAP_EXCEEDED",
        "RESULTING_BASE_PAY_OVERFLOW",
    } <= set(result["blockers"])
    pool = evaluate_pool(Pool("D01", "USD", "CC-001", str(MAX_MINOR), "0"), [result])
    assert pool["blockers"] == ["POOL_BUDGET_EXCEEDED"]
    assert pool["utilized_minor"] == str(MAX_MINOR * 2)
