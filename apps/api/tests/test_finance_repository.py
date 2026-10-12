"""Real PostgreSQL sealing and source reconstruction; all writes roll back per test."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
from meridian.finance_engine import FinanceInputError
from meridian.finance_models import CostPolicyVersion, FxRate, FxRateSet
from meridian.finance_repository import baseline_forecast, load_assignments, load_fx, load_policy
from meridian.finance_seed import (
    DEMO_POLICY,
    FX_BASE_ID,
    FX_SHOCK_ID,
    POLICY_ID,
    fixture_fx,
    seed_finance,
)
from meridian.models import uid
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from meridian.policy import canonical_hash
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

SCOPE = tuple(f"D{i:02d}" for i in range(1, 13))


def compute(db, **kwargs):
    return baseline_forecast(
        db,
        **{
            "snapshot_id": CURRENT_SNAPSHOT,
            "policy_id": POLICY_ID,
            "fx_set_id": FX_BASE_ID,
            "year": 2028,
            "scope_orgs": SCOPE,
        }
        | kwargs,
    )


def round_fraction(value):
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + int(
        2 * remainder > value.denominator
        or (2 * remainder == value.denominator and quotient % 2 == 1)
    )


def test_sealed_seed_idempotent_and_full_5k_exact_fraction_oracle(db):
    seed_finance(db)
    seed_finance(db)
    assert db.scalar(select(func.count()).select_from(CostPolicyVersion)) == 1
    assert db.scalar(select(func.count()).select_from(FxRateSet)) == 2
    assert db.scalar(select(func.count()).select_from(FxRate)) == 10
    assert load_policy(db, POLICY_ID) == DEMO_POLICY
    assert load_fx(db, FX_BASE_ID).document() == fixture_fx().document()
    snapshot, rows = load_assignments(db, CURRENT_SNAPSHOT)
    assert snapshot.row_count == 5000  # Distinct people, not 5,002 history intervals.
    assert len(rows) == 5002
    result = compute(db)
    assert result["inputs"]["snapshot_id"] == CURRENT_SNAPSHOT
    assert result["inputs"]["policy_sha256"] == DEMO_POLICY.sha256
    assert result["inputs"]["fx_sha256"] == fixture_fx().sha256
    rates = {currency: Fraction(rate) for currency, rate in fixture_fx().rates}
    benefits = {region: Fraction(value) - 1 for region, value in DEMO_POLICY.benefits}
    exponents = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KWD": 3}
    expected = dict.fromkeys(("base", "benefits", "variable", "employer_tax"), 0)
    for row in rows:
        if not row.active_at(date(2028, 1, 1)):
            continue
        base_minor = (
            Fraction(int(row.annual_pay_minor) * 100, 12 * 10 ** exponents[row.currency])
            * Fraction(row.fte)
            * rates[row.currency]
        )
        for component, factor in {
            "base": Fraction(1),
            "benefits": benefits[row.region],
            "variable": Fraction(1, 10),
            "employer_tax": Fraction(1, 20),
        }.items():
            expected[component] += round_fraction(base_minor * factor) * 12
    assert result["annual"] == {f"{key}_minor": str(value) for key, value in expected.items()} | {
        "total_minor": str(sum(expected.values()))
    }
    for month in result["months"]:
        assert month["closing_headcount"] == 5000
        assert month["closing_fte"] == "4900.0000"
        assert sum(int(org["total_minor"]) for org in month["departments"]) == int(
            month["total_minor"]
        )


def test_exact_numeric_adapter_under_small_process_context_and_fx_change(db):
    result = compute(db, scope_orgs=("D02",))
    with localcontext() as context:
        context.prec = 6
        assert load_fx(db, FX_BASE_ID).sha256 == fixture_fx().sha256
        assert compute(db, scope_orgs=("D02",)) == result
    changed = compute(db, scope_orgs=("D02",), fx_set_id=FX_SHOCK_ID)
    assert int(changed["annual"]["total_minor"]) > int(result["annual"]["total_minor"])
    assert compute(db, scope_orgs=("D02",)) == result  # Original rate set still pinned.


def test_late_source_correction_changes_future_cost_without_mutating_baseline(db):
    old = compute(db, snapshot_id=BASE_SNAPSHOT, scope_orgs=("D01",))
    new = compute(db, snapshot_id=CURRENT_SNAPSHOT, scope_orgs=("D01",))
    assert old["months"][0]["closing_headcount"] == 417
    assert new["months"][0]["closing_headcount"] == 415
    assert new["annual"]["total_minor"] != old["annual"]["total_minor"]
    assert compute(db, snapshot_id=BASE_SNAPSHOT, scope_orgs=("D01",)) == old


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE cost_policy_version SET source_label='altered' WHERE id=:policy",
        "DELETE FROM cost_policy_version WHERE id=:policy",
        "UPDATE fx_rate_set SET source_label='altered' WHERE id=:fx",
        "UPDATE fx_rate_set SET state='BUILDING' WHERE id=:fx",
        "DELETE FROM fx_rate_set WHERE id=:fx",
        "UPDATE fx_rate SET rate_decimal=2 WHERE set_id=:fx AND source_currency='EUR'",
        "DELETE FROM fx_rate WHERE set_id=:fx AND source_currency='EUR'",
        "INSERT INTO fx_rate VALUES (:fx,'GBP','USD',2)",
    ],
)
def test_financial_assumptions_sealed_in_database(db, sql):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(sql), {"policy": POLICY_ID, "fx": FX_BASE_ID})
    assert load_policy(db, POLICY_ID).sha256 == DEMO_POLICY.sha256
    assert load_fx(db, FX_BASE_ID).sha256 == fixture_fx().sha256


def new_fx(db, *, state="BUILDING", sha=None):
    fx = replace(fixture_fx(), id=uid())
    stored = FxRateSet(
        id=fx.id,
        source_label="Synthetic test fixture",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        effective_start=fx.effective_start,
        effective_end=fx.effective_end,
        reporting_currency="USD",
        state=state,
        sha256=sha or fx.sha256,
    )
    db.add(stored)
    db.flush()
    return stored


@pytest.mark.security
def test_open_fx_is_unusable_and_cannot_skip_seal_protocol(db):
    fx = new_fx(db)
    with pytest.raises(FinanceInputError, match="UNSEALED"):
        load_fx(db, fx.id)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("UPDATE fx_rate_set SET state='SEALED' WHERE id=:id"), {"id": fx.id})
    with pytest.raises(IntegrityError), db.begin_nested():
        new_fx(db, state="SEALED")
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("UPDATE fx_rate_set SET captured_at=now() WHERE id=:id"), {"id": fx.id})


@pytest.mark.security
@pytest.mark.parametrize(
    "quote,rate,currency",
    [("EUR", "1", "USD"), ("USD", "0", "EUR"), ("USD", "-1", "EUR"), ("USD", "1", "XXX")],
)
def test_invalid_rate_direction_currency_and_nonpositive_rate_rejected_by_pg(
    db, quote, rate, currency
):
    fx = new_fx(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            FxRate(
                set_id=fx.id,
                source_currency=currency,
                quote_currency=quote,
                rate_decimal=Decimal(rate),
            )
        )
        db.flush()


@pytest.mark.security
def test_fx_hash_mismatch_fails_closed_after_successful_seal(db):
    fx = new_fx(db, sha="0" * 64)
    db.add(
        FxRate(set_id=fx.id, source_currency="USD", quote_currency="USD", rate_decimal=Decimal("1"))
    )
    db.flush()
    fx.state = "SEALED"
    db.flush()
    with pytest.raises(FinanceInputError, match="FINGERPRINT_MISMATCH"):
        load_fx(db, fx.id)


@pytest.mark.security
def test_policy_unknown_keys_or_corrupted_hash_fail_closed(db):
    for definition, sha in [
        (DEMO_POLICY.document() | {"unrecognized": "1"}, None),
        (DEMO_POLICY.document(), "0" * 64),
    ]:
        policy = CostPolicyVersion(
            id=uid(),
            source_label="Synthetic invalid fixture",
            effective_date=DEMO_POLICY.effective_date,
            definition=definition,
            sha256=sha or canonical_hash(definition),
        )
        db.add(policy)
        db.flush()
        with pytest.raises(FinanceInputError, match="FINGERPRINT_MISMATCH"):
            load_policy(db, policy.id)


@pytest.mark.security
@pytest.mark.parametrize(
    "override,code",
    [
        ({"fx_set_id": "missing"}, "PINNED_FX_UNAVAILABLE"),
        ({"policy_id": "missing"}, "PINNED_POLICY_UNAVAILABLE"),
        ({"snapshot_id": "missing"}, "PINNED_SNAPSHOT_UNAVAILABLE"),
        ({"scope_orgs": ("D99",)}, "ORGANIZATION_NOT_IN_PINNED_SNAPSHOT"),
        ({"year": 2031}, "FX_DOES_NOT_COVER_FORECAST_PERIOD"),
    ],
)
def test_missing_pins_and_scope_are_hard_errors(db, override, code):
    with pytest.raises(FinanceInputError, match=code):
        compute(db, **override)
