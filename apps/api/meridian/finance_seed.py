"""Deterministic synthetic assumptions: never market FX or statutory payroll claims."""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from meridian.finance_engine import CostPolicy, RateSet
from meridian.finance_models import CostPolicyVersion, FxRate, FxRateSet
from meridian.people_seed import stable_id

POLICY_ID = stable_id("cost-policy-monthly-1")
FX_BASE_ID = stable_id("fx-synthetic-usd-1")
FX_SHOCK_ID = stable_id("fx-synthetic-usd-eur-shock-2")
DEMO_POLICY = CostPolicy(
    POLICY_ID,
    date(2026, 1, 1),
    "America/New_York",
    (("Americas", "1.25"), ("Europe", "1.30"), ("Asia Pacific", "1.20"), ("Middle East", "1.15")),
    "0.10",
    "0.05",
)


def fixture_fx(*, shock: bool = False) -> RateSet:
    return RateSet(
        FX_SHOCK_ID if shock else FX_BASE_ID,
        date(2026, 1, 1),
        date(2031, 1, 1),
        "USD",
        (
            ("USD", "1"),
            ("EUR", "1.2" if shock else "1.1"),
            ("GBP", "1.27"),
            ("JPY", "0.0067"),
            ("KWD", "3.25"),
        ),
    )


def seed_finance(db: Session) -> None:
    if db.get(CostPolicyVersion, POLICY_ID) is None:
        db.add(
            CostPolicyVersion(
                id=POLICY_ID,
                source_label="Synthetic monthly planning assumptions v1",
                effective_date=DEMO_POLICY.effective_date,
                definition=DEMO_POLICY.document(),
                sha256=DEMO_POLICY.sha256,
            )
        )
    for shock in (False, True):
        fx = fixture_fx(shock=shock)
        if db.get(FxRateSet, fx.id) is not None:
            continue
        stored = FxRateSet(
            id=fx.id,
            source_label="Synthetic EUR sensitivity fixture"
            if shock
            else "Synthetic USD planning fixture",
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            effective_start=fx.effective_start,
            effective_end=fx.effective_end,
            reporting_currency=fx.reporting_currency,
            state="BUILDING",
            sha256=fx.sha256,
        )
        db.add(stored)
        db.flush()
        db.add_all(
            [
                FxRate(
                    set_id=fx.id,
                    source_currency=currency,
                    quote_currency=fx.reporting_currency,
                    rate_decimal=Decimal(rate),
                )
                for currency, rate in fx.rates
            ]
        )
        db.flush()
        stored.state = "SEALED"
        db.flush()
