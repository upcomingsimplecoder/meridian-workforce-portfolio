"""Real PostgreSQL guideline seals and exact source freeze without an API shortcut."""

from datetime import date
from fractions import Fraction

import pytest
from meridian.compensation_engine import CycleTerms, calculate_award
from meridian.compensation_models import CompGuidelineVersion
from meridian.compensation_repository import load_eligibility, load_guideline
from meridian.compensation_seed import DEMO_GUIDELINE, GUIDELINE_ID, seed_compensation
from meridian.finance_engine import FinanceInputError
from meridian.finance_repository import load_fx
from meridian.finance_seed import FX_BASE_ID, FX_SHOCK_ID
from meridian.models import uid
from meridian.people_models import AuthzPolicy, EmployeeVersion, Snapshot
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from meridian.policy import canonical_hash
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

TERMS = CycleTerms(
    eligibility_date=date(2028, 3, 31),
    merit_effective_date=date(2028, 5, 1),
    bonus_service_start=date(2028, 1, 1),
    bonus_service_end=date(2029, 1, 1),
    bonus_payment_date=date(2029, 1, 15),
)


def roster(db, *, selected_ids=("SYN-000025", "SYN-000037", "SYN-000049"), **override):
    return load_eligibility(
        db,
        **{
            "snapshot_id": CURRENT_SNAPSHOT,
            "guideline_id": GUIDELINE_ID,
            "terms": TERMS,
            "selected_ids": selected_ids,
        }
        | override,
    )


def test_guideline_seed_is_idempotent_and_old_authz_policy_is_unchanged(db):
    before = db.get(AuthzPolicy, "authz-v1").sha256
    seed_compensation(db)
    seed_compensation(db)
    db.flush()
    assert db.scalar(select(func.count()).select_from(CompGuidelineVersion)) == 1
    assert load_guideline(db, GUIDELINE_ID) == DEMO_GUIDELINE
    assert db.get(AuthzPolicy, "authz-v1").sha256 == before


def test_selected_population_captures_late_source_transfer_and_original_snapshot_unchanged(db):
    old = roster(db, snapshot_id=BASE_SNAPSHOT)
    corrected = roster(db)
    old_worker = next(row for row in old if row.employee_id == "SYN-000037")
    new_worker = next(row for row in corrected if row.employee_id == "SYN-000037")
    assert old_worker.org_id == "D01" and new_worker.org_id == "D02"
    assert old_worker.proposer_id == "manager-a" and new_worker.proposer_id == "manager-b"
    assert [link.employee_id for link in old_worker.chain] == [
        "SYN-000037",
        "SYN-000025",
        "SYN-000001",
    ]
    assert [link.employee_id for link in new_worker.chain] == [
        "SYN-000037",
        "SYN-000014",
        "SYN-000002",
    ]
    assert len(new_worker.history) == 2 and len(old_worker.history) == 1
    assert old_worker.sha256 != new_worker.sha256
    assert roster(db, snapshot_id=BASE_SNAPSHOT) == old
    # Corrected cross-department descendant SYN49 remains a D01 employee managed by B.
    cross = next(row for row in corrected if row.employee_id == "SYN-000049")
    assert cross.org_id == "D01" and cross.proposer_id == "manager-b"
    assert cross.chain[-1].employee_id == "SYN-000002"


def test_actual_5k_freeze_preserves_all_inputs_without_reading_newer_publications(db):
    ids = tuple(
        db.scalars(
            select(EmployeeVersion.employee_id)
            .where(EmployeeVersion.snapshot_id == CURRENT_SNAPSHOT)
            .distinct()
        )
    )
    result = roster(db, selected_ids=ids)
    assert len(result) == 5000
    assert sum(row.eligible for row in result) == 832  # Two trusted manager accounts only.
    assert {row.proposer_id for row in result if row.eligible} == {"manager-a", "manager-b"}
    assert sum(len(row.history) for row in result) == 5002
    assert all(row.history and row.reason_codes == () for row in result if row.eligible)
    assert db.get(Snapshot, CURRENT_SNAPSHOT).row_count == 5000


def test_832_actual_eligible_awards_reconcile_with_independent_fraction_oracle(db):
    ids = tuple(
        db.scalars(
            select(EmployeeVersion.employee_id)
            .where(EmployeeVersion.snapshot_id == CURRENT_SNAPSHOT)
            .distinct()
        )
    )
    population = roster(db, selected_ids=ids)
    fx = load_fx(db, FX_BASE_ID)
    totals = {
        "USD": {"annualized": 0, "first_year": 0, "bonus": 0},
        "EUR": {"annualized": 0, "first_year": 0, "bonus": 0},
    }
    reporting = 0

    def rounded(value):
        whole, remainder = divmod(value.numerator, value.denominator)
        return whole + int(
            2 * remainder > value.denominator or (2 * remainder == value.denominator and whole % 2)
        )

    for row in population:
        if not row.eligible:
            continue
        merit, bonus = int(row.base_pay_minor) * 3 // 100, int(row.base_pay_minor) * 8 // 100
        expected_merit = rounded(Fraction(merit) * Fraction(row.fte))
        expected_bonus = rounded(Fraction(bonus) * Fraction(row.fte))
        expected_first_year = rounded(Fraction(merit, 12) * Fraction(row.fte)) * 8
        expected_reporting = rounded(
            Fraction(expected_merit + expected_bonus) * Fraction(dict(fx.rates)[row.currency])
        )
        actual = calculate_award(
            row, DEMO_GUIDELINE, TERMS, fx, merit_minor=str(merit), bonus_minor=str(bonus)
        )
        assert actual["annualized_merit_minor"] == str(expected_merit)
        assert actual["bonus_payable_minor"] == str(expected_bonus)
        assert actual["merit_first_year_minor"] == str(expected_first_year)
        assert actual["reporting_pool_charge_minor"] == str(expected_reporting)
        assert actual["blockers"] == [] and actual["exceptions"] == []
        totals[row.currency]["annualized"] += expected_merit
        totals[row.currency]["bonus"] += expected_bonus
        totals[row.currency]["first_year"] += expected_first_year
        reporting += expected_reporting
    assert totals == {
        "USD": {"annualized": 147945000, "bonus": 394520000, "first_year": 98630000},
        "EUR": {"annualized": 141430200, "bonus": 377147200, "first_year": 94286800},
    }
    assert reporting == 1112900140


def test_source_read_exact_amounts_and_pinned_fx_change_only_reporting_result(db):
    row = roster(db, selected_ids=("SYN-000038",))[0]
    assert row.currency == "EUR" and row.proposer_id == "manager-b"
    old = calculate_award(
        row,
        DEMO_GUIDELINE,
        TERMS,
        load_fx(db, FX_BASE_ID),
        merit_minor="123456",
        bonus_minor="654321",
    )
    new = calculate_award(
        row,
        DEMO_GUIDELINE,
        TERMS,
        load_fx(db, FX_SHOCK_ID),
        merit_minor="123456",
        bonus_minor="654321",
    )
    assert old["pool_charge_minor"] == "777777" == new["pool_charge_minor"]
    assert old["reporting_pool_charge_minor"] == "855555"
    assert new["reporting_pool_charge_minor"] == "933332"
    assert old["inputs"]["fx_sha256"] != new["inputs"]["fx_sha256"]


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE comp_guideline_version SET source_label='altered' WHERE id=:id",
        "UPDATE comp_guideline_version SET definition='{}' WHERE id=:id",
        "UPDATE comp_guideline_version SET sha256=repeat('0',64) WHERE id=:id",
        "UPDATE comp_guideline_version SET effective_date='2028-01-01' WHERE id=:id",
        "DELETE FROM comp_guideline_version WHERE id=:id",
    ],
)
def test_guideline_cannot_be_changed_or_deleted_even_using_direct_sql(db, sql):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(sql), {"id": GUIDELINE_ID})
    assert load_guideline(db, GUIDELINE_ID) == DEMO_GUIDELINE


@pytest.mark.security
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("hash", "FINGERPRINT_MISMATCH"),
        ("extra_key", "INVALID_PINNED"),
        ("float", "INVALID_PINNED"),
        ("date", "FINGERPRINT_MISMATCH"),
        ("omitted_default", "FINGERPRINT_MISMATCH"),
        ("wrong_cap", "INVALID_PINNED"),
    ],
)
def test_inserted_corrupt_definition_fails_closed_on_load(db, mutation, code):
    document = DEMO_GUIDELINE.document()
    effective = DEMO_GUIDELINE.effective_date
    if mutation == "extra_key":
        document["unknown"] = "secret"
    elif mutation == "float":
        document["bonus_guideline_rate"] = 0.1
    elif mutation == "date":
        effective = date(2028, 1, 1)
    elif mutation == "omitted_default":
        document.pop("rounding")
    elif mutation == "wrong_cap":
        document["merit_hard_cap_rate"] = "0.001"
    stored = CompGuidelineVersion(
        id=uid(),
        source_label="Synthetic invalid fixture",
        effective_date=effective,
        definition=document,
        sha256="0" * 64 if mutation == "hash" else canonical_hash(document),
    )
    db.add(stored)
    db.flush()
    with pytest.raises(FinanceInputError, match=code):
        load_guideline(db, stored.id)


@pytest.mark.security
@pytest.mark.parametrize(
    "override,code",
    [
        ({"snapshot_id": "missing"}, "PINNED_SNAPSHOT_UNAVAILABLE"),
        ({"guideline_id": "missing"}, "PINNED_COMPENSATION_GUIDELINE_UNAVAILABLE"),
        ({"selected_ids": ("SYN-999999",)}, "NOT_IN_PINNED_SNAPSHOT"),
        ({"selected_ids": ()}, "INVALID_COMPENSATION_POPULATION"),
    ],
)
def test_missing_source_pins_and_invalid_population_fail_closed(db, override, code):
    with pytest.raises(FinanceInputError, match=code):
        roster(db, **override)
