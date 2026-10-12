"""Load sealed source/assumption versions, never a mutable current-rate lookup."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.finance_engine import (
    MAX_HISTORY_ROWS,
    CostAssignment,
    CostPolicy,
    FinanceInputError,
    RateSet,
    calculate_forecast,
)
from meridian.finance_models import CostPolicyVersion, FxRate, FxRateSet
from meridian.people_models import EmployeeVersion, OrgVersion, Snapshot
from meridian.policy import canonical_hash
from meridian.scenario_operations import PlanningAssignment


def exact_decimal(value: Any) -> str:
    # ORM Numeric uses Decimal; canonicalize fixed scale without a float or Decimal.normalize()
    # (normalize consults the process-wide context and can silently lose significant digits).
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def load_policy(db: Session, policy_id: str) -> CostPolicy:
    stored = db.get(CostPolicyVersion, policy_id)
    if stored is None:
        raise FinanceInputError("PINNED_POLICY_UNAVAILABLE")
    definition = stored.definition
    if stored.sha256 != canonical_hash(definition):
        raise FinanceInputError("PINNED_POLICY_FINGERPRINT_MISMATCH")
    try:
        policy = CostPolicy(
            stored.id,
            stored.effective_date,
            definition["timezone"],
            tuple(sorted(definition["benefit_multipliers"].items())),
            definition["variable_accrual_rate"],
            definition["employer_tax_rate"],
        )
        # Exact document equality also rejects unknown keys, hidden floats and date/metadata drift.
        if policy.document() != definition or policy.sha256 != stored.sha256:
            raise FinanceInputError("PINNED_POLICY_FINGERPRINT_MISMATCH")
        policy.validate(stored.effective_date)
    except (KeyError, TypeError, AttributeError) as error:
        raise FinanceInputError("INVALID_PINNED_POLICY") from error
    return policy


def load_fx(db: Session, set_id: str) -> RateSet:
    stored = db.get(FxRateSet, set_id)
    if stored is None or stored.state != "SEALED":
        raise FinanceInputError("PINNED_FX_UNAVAILABLE_OR_UNSEALED")
    rows = list(
        db.scalars(select(FxRate).where(FxRate.set_id == set_id).order_by(FxRate.source_currency))
    )
    if any(row.quote_currency != stored.reporting_currency for row in rows):
        raise FinanceInputError("PINNED_FX_QUOTE_MISMATCH")
    fx = RateSet(
        stored.id,
        stored.effective_start,
        stored.effective_end,
        stored.reporting_currency,
        tuple((row.source_currency, exact_decimal(row.rate_decimal)) for row in rows),
    )
    fx.validate(stored.effective_start, stored.effective_end)
    if fx.sha256 != stored.sha256:
        raise FinanceInputError("PINNED_FX_FINGERPRINT_MISMATCH")
    return fx


def load_assignments(db: Session, snapshot_id: str) -> tuple[Snapshot, list[CostAssignment]]:
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.status != "PUBLISHED":
        raise FinanceInputError("PINNED_SNAPSHOT_UNAVAILABLE_OR_UNSEALED")
    rows = list(
        db.scalars(
            select(EmployeeVersion)
            .where(EmployeeVersion.snapshot_id == snapshot_id)
            .limit(MAX_HISTORY_ROWS + 1)
        )
    )
    if len(rows) > MAX_HISTORY_ROWS:
        raise FinanceInputError("FORECAST_HISTORY_WORK_LIMIT")
    if len({row.employee_id for row in rows}) != snapshot.row_count:
        raise FinanceInputError("PINNED_SNAPSHOT_COUNT_MISMATCH")
    return snapshot, [
        CostAssignment(
            row.employee_id,
            row.id,
            row.source_batch_id,
            row.org_id,
            row.region,
            row.valid_from,
            row.valid_to,
            row.status,
            exact_decimal(row.annual_pay_minor),
            row.currency,
            exact_decimal(row.fte),
        )
        for row in rows
    ]


def baseline_forecast(
    db: Session,
    *,
    snapshot_id: str,
    policy_id: str,
    fx_set_id: str,
    year: int,
    scope_orgs: tuple[str, ...],
) -> dict[str, Any]:
    snapshot, rows = load_assignments(db, snapshot_id)
    known_orgs = set(
        db.scalars(select(OrgVersion.org_id).where(OrgVersion.snapshot_id == snapshot_id))
    )
    if not set(scope_orgs) <= known_orgs:
        raise FinanceInputError("ORGANIZATION_NOT_IN_PINNED_SNAPSHOT")
    return calculate_forecast(
        rows,
        load_policy(db, policy_id),
        load_fx(db, fx_set_id),
        snapshot_id=snapshot.id,
        snapshot_sha256=snapshot.sha256,
        year=year,
        scope_orgs=scope_orgs,
    )


def load_planning_history(
    db: Session, snapshot_id: str
) -> tuple[Snapshot, list[PlanningAssignment]]:
    snapshot, costs = load_assignments(db, snapshot_id)
    metadata = {
        row.id: row
        for row in db.execute(
            select(
                EmployeeVersion.id,
                EmployeeVersion.position_id,
                EmployeeVersion.manager_id,
                EmployeeVersion.grade,
            )
            .where(EmployeeVersion.snapshot_id == snapshot_id)
            .limit(MAX_HISTORY_ROWS + 1)
        )
    }
    return snapshot, [
        PlanningAssignment(
            cost,
            metadata[cost.version_id].position_id,
            metadata[cost.version_id].manager_id,
            metadata[cost.version_id].grade,
        )
        for cost in costs
    ]
