"""Independent Fraction oracle and hand-calculated goldens, no engine-derived expectations."""

import calendar
import random
from dataclasses import replace
from datetime import date
from decimal import Inexact, localcontext
from fractions import Fraction

import pytest
from meridian.finance_engine import (
    COMPONENTS,
    CostAssignment,
    CostPolicy,
    FinanceInputError,
    RateSet,
    calculate_forecast,
)

POLICY = CostPolicy(
    "policy-1",
    date(2026, 1, 1),
    "America/New_York",
    (("Americas", "1.25"), ("Europe", "1.30"), ("Asia Pacific", "1.20"), ("Middle East", "1.15")),
    "0.10",
    "0.05",
)
FX = RateSet(
    "fx-1",
    date(2026, 1, 1),
    date(2031, 1, 1),
    "USD",
    (("USD", "1"), ("EUR", "1.10"), ("GBP", "1.27"), ("JPY", "0.0067"), ("KWD", "3.25")),
)
ZERO_POLICY = replace(
    POLICY,
    benefits=tuple((region, "1") for region, _ in POLICY.benefits),
    variable_rate="0",
    employer_tax_rate="0",
)
ROW = CostAssignment(
    "SYN-000001",
    "version-1",
    "batch-1",
    "D01",
    "Americas",
    date(2027, 1, 1),
    None,
    "ACTIVE",
    "12000000",
    "USD",
    "1.0000",
)


def forecast(rows=None, *, policy=POLICY, fx=FX, year=2027, scope=("D01",)):
    return calculate_forecast(
        [ROW] if rows is None else rows,
        policy,
        fx,
        snapshot_id="snapshot-1",
        snapshot_sha256="a" * 64,
        year=year,
        scope_orgs=scope,
    )


def rounded_fraction(value: Fraction) -> int:
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + int(
        2 * remainder > value.denominator
        or (2 * remainder == value.denominator and quotient % 2 == 1)
    )


def test_annual_salary_fte_once_and_additive_components():
    result = forecast()
    assert result["annual"] == {
        "base_minor": "12000000",
        "benefits_minor": "3000000",
        "variable_minor": "1200000",
        "employer_tax_minor": "600000",
        "total_minor": "16800000",
    }
    for month in result["months"]:
        assert month["total_minor"] == "1400000"
        assert month["closing_headcount"] == 1
        assert month["average_fte"] == "1.0000"
    half_time = forecast([replace(ROW, fte="0.5000")])
    assert half_time["annual"]["total_minor"] == "8400000"
    assert half_time["months"][0]["closing_fte"] == "0.5000"


def test_leap_day_hire_golden():
    result = forecast([replace(ROW, valid_from=date(2028, 2, 29), fte="0.5000")], year=2028)
    february = result["months"][1]
    # USD 120,000 / 12 / 29 * 0.5 = USD 172.413793... base.
    assert february["base_minor"] == "17241"
    assert february["benefits_minor"] == "4310"
    assert february["variable_minor"] == "1724"
    assert february["employer_tax_minor"] == "862"
    assert february["total_minor"] == "24137"
    assert february["opening_headcount"] == 0
    assert february["closing_headcount"] == 1
    assert february["average_fte"] == "0.0172"
    assert result["annual"]["base_minor"] == "5017241"


@pytest.mark.parametrize(
    "year,month,days", [(2027, 2, 28), (2028, 2, 29), (2027, 4, 30), (2027, 1, 31)]
)
def test_last_day_hire_and_exclusive_departure(year, month, days):
    start = date(year, month, days)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last_day = forecast([replace(ROW, valid_from=start, valid_to=next_month)], year=year)
    assert int(last_day["months"][month - 1]["base_minor"]) == rounded_fraction(
        Fraction(1000000, days)
    )
    departed = forecast([replace(ROW, valid_from=date(year, month, 1), valid_to=start)], year=year)
    assert departed["months"][month - 1]["closing_headcount"] == 0
    assert int(departed["months"][month - 1]["base_minor"]) == rounded_fraction(
        Fraction(1000000 * (days - 1), days)
    )


@pytest.mark.parametrize(
    "minor,currency,region,monthly_base",
    [
        ("12000000", "USD", "Americas", "1000000"),
        ("12000000", "EUR", "Europe", "1100000"),
        ("12000000", "GBP", "Europe", "1270000"),
        ("12000000", "JPY", "Asia Pacific", "670000"),
        ("120000000", "KWD", "Middle East", "3250000"),
    ],
)
def test_original_currency_exponents(minor, currency, region, monthly_base):
    result = forecast([replace(ROW, annual_pay_minor=minor, currency=currency, region=region)])
    assert {month["base_minor"] for month in result["months"]} == {monthly_base}


@pytest.mark.parametrize(
    "currency,rate,expected", [("JPY", "150", "1500000"), ("KWD", "0.3", "3000000")]
)
def test_reporting_currency_exponents(currency, rate, expected):
    fx = replace(FX, reporting_currency=currency, rates=((currency, "1"), ("USD", rate)))
    result = forecast(fx=fx)
    assert result["months"][0]["base_minor"] == expected
    assert result["exponent"] == {"JPY": 0, "KWD": 3}[currency]


@pytest.mark.parametrize("annual,monthly", [("6", "0"), ("18", "2"), ("30", "2"), ("42", "4")])
def test_half_even_ties_and_segmentation_independence(annual, monthly):
    row = replace(ROW, annual_pay_minor=annual)
    whole = forecast([row], policy=ZERO_POLICY)
    split = forecast(
        [
            replace(row, valid_to=date(2027, 1, 2)),
            replace(row, valid_from=date(2027, 1, 2), version_id="split-2"),
        ],
        policy=ZERO_POLICY,
    )
    assert whole["months"] == split["months"]
    assert whole["months"][0]["base_minor"] == monthly
    assert whole["sha256"] != split["sha256"]  # Same results, different source provenance.


def test_transfer_allocation_scope_projection_and_tie_break():
    rows = [
        replace(ROW, annual_pay_minor="12", valid_to=date(2027, 4, 16)),
        replace(
            ROW, annual_pay_minor="12", org_id="D02", valid_from=date(2027, 4, 16), version_id="v2"
        ),
    ]
    whole = forecast(rows, policy=ZERO_POLICY, scope=("D02", "D01"))
    first = forecast(rows, policy=ZERO_POLICY, scope=("D01",))
    second = forecast(rows, policy=ZERO_POLICY, scope=("D02",))
    april = whole["months"][3]
    assert april["base_minor"] == "1"
    assert [department["base_minor"] for department in april["departments"]] == ["1", "0"]
    assert first["months"][3]["base_minor"] == "1"
    assert second["months"][3]["base_minor"] == "0"
    for a, b, c in zip(whole["months"], first["months"], second["months"], strict=True):
        assert int(a["total_minor"]) == int(b["total_minor"]) + int(c["total_minor"])


def test_20_digit_amount_no_precision_loss_and_context_isolation():
    rows = [replace(ROW, annual_pay_minor="99999999999999999999")]
    expected_month = rounded_fraction(Fraction(99999999999999999999, 12))
    normal = forecast(rows, policy=ZERO_POLICY)
    with localcontext() as context:
        context.prec = 6
        context.rounding = "ROUND_DOWN"
        changed_context = forecast(rows, policy=ZERO_POLICY)
        assert context.prec == 6  # Calculation restores the caller's context.
    assert changed_context == normal
    assert normal["months"][0]["base_minor"] == str(expected_month)
    assert normal["annual"]["base_minor"] == str(expected_month * 12)


def test_caller_decimal_traps_and_exponent_limits_cannot_change_results():
    rows = [replace(ROW, valid_from=date(2028, 2, 29), fte="0.5000")]
    expected = forecast(rows, year=2028)
    with localcontext() as context:
        context.traps[Inexact] = True
        context.Emax = 2
        context.Emin = -2
        assert forecast(rows, year=2028) == expected
        assert context.traps[Inexact] is True
        assert context.Emax == 2


def test_repeat_order_and_policy_fx_fingerprints():
    rows = [ROW, replace(ROW, employee_id="SYN-000002", version_id="v2", currency="EUR")]
    before = forecast(rows)
    assert before == forecast(list(reversed(rows)))
    assert before == forecast(
        rows,
        policy=replace(POLICY, benefits=tuple(reversed(POLICY.benefits))),
        fx=replace(FX, rates=tuple(reversed(FX.rates))),
    )
    changed = forecast(
        rows,
        fx=replace(
            FX, id="fx-2", rates=tuple((c, "1.20" if c == "EUR" else r) for c, r in FX.rates)
        ),
    )
    assert before["annual"]["base_minor"] == "25200000"
    assert changed["annual"]["base_minor"] == "26400000"
    assert before["sha256"] != changed["sha256"]
    earlier_policy = replace(POLICY, effective_date=date(2025, 1, 1))
    assert earlier_policy.sha256 != POLICY.sha256


def test_randomized_forecasts_against_independent_fraction_oracle():
    rng = random.Random(901)
    exponents = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KWD": 3}
    rows = []
    for i in range(1, 61):
        currency = rng.choice(list(exponents))
        month = rng.randint(1, 12)
        days = calendar.monthrange(2028, month)[1]
        left = rng.randint(1, days)
        right = rng.randint(left + 1, days + 1)
        rows.append(
            replace(
                ROW,
                employee_id=f"SYN-{i:06d}",
                version_id=f"v{i}",
                currency=currency,
                annual_pay_minor=str(rng.randint(0, 10**20 - 1)),
                fte=rng.choice(["0.1234", "0.8000", "1.0000"]),
                valid_from=date(2028, month, left),
                valid_to=date(2028 + (month == 12), month % 12 + 1, 1)
                if right == days + 1
                else date(2028, month, right),
            )
        )
    output = forecast(rows, year=2028)
    rates = {currency: Fraction(rate) for currency, rate in FX.rates}
    factors = {
        "base": Fraction(1),
        "benefits": Fraction(1, 4),
        "variable": Fraction(1, 10),
        "employer_tax": Fraction(1, 20),
    }
    for month in output["months"]:
        month_number = date.fromisoformat(month["month"]).month
        days = calendar.monthrange(2028, month_number)[1]
        for component in COMPONENTS:
            expected = 0
            for row in rows:
                if row.valid_from.month != month_number:
                    continue
                active_days = (row.valid_to - row.valid_from).days
                original = Fraction(
                    int(row.annual_pay_minor), 10 ** exponents[row.currency] * 12 * days
                )
                exact = (
                    original
                    * active_days
                    * Fraction(row.fte)
                    * rates[row.currency]
                    * factors[component]
                    * 100
                )
                expected += rounded_fraction(exact)
            assert int(month[f"{component}_minor"]) == expected
    assert sum(int(month["total_minor"]) for month in output["months"]) == int(
        output["annual"]["total_minor"]
    )


@pytest.mark.security
@pytest.mark.parametrize(
    "field,value,code",
    [
        ("annual_pay_minor", 12000000, "INVALID_DECIMAL_STRING"),
        ("annual_pay_minor", "1.0", "DECIMAL_OUT_OF_RANGE"),
        ("annual_pay_minor", "-1", "INVALID_DECIMAL_STRING"),
        ("annual_pay_minor", "100000000000000000000", "DECIMAL_OUT_OF_RANGE"),
        ("annual_pay_minor", "NaN", "INVALID_DECIMAL_STRING"),
        ("fte", "0", "DECIMAL_OUT_OF_RANGE"),
        ("fte", "1.1", "DECIMAL_OUT_OF_RANGE"),
        ("fte", "0.12345", "DECIMAL_OUT_OF_RANGE"),
        ("fte", 0.5, "INVALID_DECIMAL_STRING"),
        ("currency", "XXX", "UNSUPPORTED_CURRENCY_OR_REGION"),
        ("employee_id", "REAL-000001", "NON_SYNTHETIC_IDENTITY"),
        ("org_id", "G01", "INVALID_ASSIGNMENT_ORGANIZATION"),
        ("status", "ON_LEAVE", "INVALID_EMPLOYMENT_STATUS"),
    ],
)
def test_invalid_financial_inputs_are_safe_errors(field, value, code):
    with pytest.raises(FinanceInputError, match=f"^{code}$"):
        forecast([replace(ROW, **{field: value})])


@pytest.mark.security
def test_missing_and_outdated_fx_and_invalid_policy_hard_fail():
    with pytest.raises(FinanceInputError, match="MISSING_PINNED_FX_RATE"):
        forecast([replace(ROW, currency="EUR")], fx=replace(FX, rates=(("USD", "1"),)))
    for fx, code in [
        (replace(FX, effective_start=date(2027, 1, 2)), "FX_DOES_NOT_COVER_FORECAST_PERIOD"),
        (replace(FX, effective_end=date(2027, 12, 31)), "FX_DOES_NOT_COVER_FORECAST_PERIOD"),
        (replace(FX, rates=(("USD", "0"),)), "DECIMAL_OUT_OF_RANGE"),
        (replace(FX, rates=(("USD", "2"),)), "REPORTING_CURRENCY_REQUIRES_IDENTITY_RATE"),
        (replace(FX, rates=(("USD", "1"), ("USD", "1"))), "INVALID_FX_SET"),
    ]:
        with pytest.raises(FinanceInputError, match=code):
            forecast(fx=fx)
    for policy in [
        replace(POLICY, effective_date=date(2027, 1, 2)),
        replace(POLICY, timezone="UTC"),
        replace(POLICY, benefits=(("Americas", "1"),)),
        replace(POLICY, variable_rate="1.1"),
    ]:
        with pytest.raises(FinanceInputError):
            forecast(policy=policy)


@pytest.mark.security
def test_overlaps_invalid_periods_scopes_and_years_rejected():
    with pytest.raises(FinanceInputError, match="OVERLAPPING_COST_ASSIGNMENTS"):
        forecast([ROW, replace(ROW, version_id="v2", valid_from=date(2027, 2, 1))])
    with pytest.raises(FinanceInputError, match="INVALID_ASSIGNMENT_PERIOD"):
        forecast([replace(ROW, valid_to=ROW.valid_from)])
    for scope in [(), ("D01", "D01"), ("*",)]:
        with pytest.raises(FinanceInputError, match="INVALID_FORECAST_SCOPE"):
            forecast(scope=scope)
    with pytest.raises(FinanceInputError, match="UNSUPPORTED_FORECAST_YEAR"):
        forecast(year=2200)


def test_exited_and_empty_scope_have_exact_zero_cost():
    empty = forecast([replace(ROW, status="EXITED")])
    assert all(value == "0" for value in empty["annual"].values())
    assert all(month["closing_headcount"] == 0 for month in empty["months"])
    assert forecast([])["annual"] == empty["annual"]


@pytest.mark.security
def test_history_work_budget_rejects_before_costing():
    with pytest.raises(FinanceInputError, match="FORECAST_HISTORY_WORK_LIMIT"):
        forecast([ROW] * 100001)
