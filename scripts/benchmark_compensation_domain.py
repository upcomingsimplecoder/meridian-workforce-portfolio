"""Measured local compensation input/calculation benchmark; no API or workflow claim."""

import json
import platform
import statistics
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

from meridian.compensation_engine import CycleTerms, calculate_award
from meridian.compensation_repository import load_eligibility, load_guideline
from meridian.compensation_seed import GUIDELINE_ID
from meridian.db import SessionLocal
from meridian.finance_repository import load_fx
from meridian.finance_seed import FX_BASE_ID
from meridian.people_models import EmployeeVersion, Snapshot
from meridian.people_seed import CURRENT_SNAPSHOT
from meridian.policy import canonical_hash
from sqlalchemy import select

TERMS = CycleTerms(
    eligibility_date=date(2028, 3, 31),
    merit_effective_date=date(2028, 5, 1),
    bonus_service_start=date(2028, 1, 1),
    bonus_service_end=date(2029, 1, 1),
    bonus_payment_date=date(2029, 1, 15),
)


def summarize(awards):
    currencies = defaultdict(lambda: defaultdict(int))
    for award in awards:
        for field in (
            "annualized_merit_minor",
            "merit_first_year_minor",
            "bonus_payable_minor",
            "pool_charge_minor",
        ):
            currencies[award["currency"]][field] += int(award[field])
    return {
        "award_count": len(awards),
        "totals_by_currency": {
            currency: {field: str(amount) for field, amount in sorted(values.items())}
            for currency, values in sorted(currencies.items())
        },
        "reporting_currency": "USD",
        "reporting_pool_charge_minor": str(
            sum(int(row["reporting_pool_charge_minor"]) for row in awards)
        ),
        "awards_sha256": canonical_hash(awards),
    }


def main():
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, CURRENT_SNAPSHOT)
        guideline = load_guideline(db, GUIDELINE_ID)
        fx = load_fx(db, FX_BASE_ID)
        ids = tuple(
            db.scalars(
                select(EmployeeVersion.employee_id)
                .where(EmployeeVersion.snapshot_id == CURRENT_SNAPSHOT)
                .distinct()
                .order_by(EmployeeVersion.employee_id)
            )
        )
        times = defaultdict(list)
        summaries = []
        for sample in range(23):
            start = time.perf_counter()
            population = load_eligibility(
                db,
                snapshot_id=CURRENT_SNAPSHOT,
                guideline_id=GUIDELINE_ID,
                terms=TERMS,
                selected_ids=ids,
            )
            loaded = time.perf_counter()
            awards = [
                calculate_award(
                    row,
                    guideline,
                    TERMS,
                    fx,
                    merit_minor=str(int(row.base_pay_minor) * 3 // 100),
                    bonus_minor=str(int(row.base_pay_minor) * 8 // 100),
                )
                for row in population
                if row.eligible
            ]
            completed = time.perf_counter()
            summaries.append(summarize(awards))
            if sample >= 3:
                times["source_load_and_freeze"].append((loaded - start) * 1000)
                times["832_award_calculations"].append((completed - loaded) * 1000)
                times["combined"].append((completed - start) * 1000)
        if len({canonical_hash(summary) for summary in summaries}) != 1:
            raise RuntimeError("Deterministic compensation result changed between samples")
        result = {
            "claim_scope": "warm local domain + PostgreSQL input adapter; no persisted compensation cycle, API/browser/worker or hosted measurement",
            "hardware": {
                "machine": platform.machine(),
                "processor": "Apple M1",
                "cpu_cores": 8,
                "ram_gib": 8,
                "os": platform.platform(),
            },
            "dataset": {
                "snapshot_id": snapshot.id,
                "snapshot_sha256": snapshot.sha256,
                "people": len(ids),
                "history_intervals": sum(len(row.history) for row in population),
                "eligible": len(awards),
                "ineligible": len(population) - len(awards),
                "reason": "Only two trusted manager accounts exist; other selected workers retain explicit ineligible reasons",
                "terms": TERMS.document(),
                "guideline_id": GUIDELINE_ID,
                "guideline_sha256": guideline.sha256,
                "fx_set_id": FX_BASE_ID,
                "fx_sha256": fx.sha256,
            },
            "conditions": {
                "concurrency": 1,
                "warmups": 3,
                "samples_per_path": 20,
                "database": "native PostgreSQL 17",
                "awards": "3% merit and 8% bonus targets of frozen full-time reference salary, input rounded down to integer original minor units",
            },
            "latency_ms": {
                name: {
                    "p50": round(statistics.median(values), 3),
                    "p95": round(sorted(values)[18], 3),
                    "maximum": round(max(values), 3),
                }
                for name, values in times.items()
            },
            "observed_output": summaries[-1],
        }
    target = Path("docs/evidence/p05-domain-performance.json")
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "dataset": result["dataset"],
                "latency_ms": result["latency_ms"],
                "observed_output": result["observed_output"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
