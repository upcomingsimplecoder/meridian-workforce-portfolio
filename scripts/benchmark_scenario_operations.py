"""A real seven-operation scenario calculation against the sealed 5k source (domain only)."""

import json
import math
import platform
import time
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid5

from meridian.db import SessionLocal
from meridian.finance_engine import calculate_forecast
from meridian.finance_repository import load_fx, load_planning_history, load_policy
from meridian.finance_seed import FX_BASE_ID, POLICY_ID
from meridian.people_models import OrgVersion
from meridian.people_seed import CURRENT_SNAPSHOT, stable_id
from meridian.scenario_operations import (
    OperationList,
    PlanningRules,
    apply_operations,
    validate_budget,
)
from sqlalchemy import select

namespace = UUID(stable_id("scenario-benchmark"))
hire_id = str(uuid5(namespace, "hire"))
definitions = [
    {
        "kind": "hire",
        "effective_date": "2028-02-29",
        "employee_id": "SYN-900001",
        "position_id": "POS-900001",
        "org_id": "D01",
        "manager_id": "SYN-000001",
        "grade": "L1",
        "annual_pay_minor": "12000000",
        "currency": "USD",
        "fte": "0.5000",
    },
    {
        "kind": "transfer",
        "effective_date": "2028-03-01",
        "employee_id": "SYN-000037",
        "org_id": "D01",
        "manager_id": "SYN-000001",
    },
    {"kind": "vacancy_delay", "effective_date": "2028-03-29", "hire_operation_id": hire_id},
    {
        "kind": "benefit_multiplier",
        "effective_date": "2028-04-01",
        "org_id": "D01",
        "multiplier": "1.28",
    },
    {"kind": "merit_assumption", "effective_date": "2028-07-01", "org_id": "D01", "rate": "0.03"},
    {
        "kind": "promotion",
        "effective_date": "2028-08-01",
        "employee_id": "SYN-000049",
        "grade": "L10",
        "annual_pay_minor": "24000000",
    },
    {"kind": "departure", "effective_date": "2028-09-01", "employee_id": "SYN-005000"},
]
operations = OperationList.validate_python(
    [
        item | {"id": str(uuid5(namespace, item["kind"])), "reason": "Synthetic benchmark plan"}
        for item in definitions
    ]
)
scope = tuple(f"D{i:02d}" for i in range(1, 13))
samples: dict[str, list[float]] = {"operations_only": [], "source_load_operations_and_engine": []}
digests = set()
with SessionLocal() as db:
    for iteration in range(23):
        begin = time.perf_counter()
        snapshot, source = load_planning_history(db, CURRENT_SNAPSHOT)
        # Fixture org versions have one immutable region each; this is not a general date adapter.
        region_rows = db.execute(
            select(OrgVersion.org_id, OrgVersion.region).where(
                OrgVersion.snapshot_id == snapshot.id
            )
        ).all()
        regions: dict[str, str] = {}
        for org, region in region_rows:
            assert org not in regions or regions[org] == region
            regions[org] = region
        policy, fx = load_policy(db, POLICY_ID), load_fx(db, FX_BASE_ID)
        start_operations = time.perf_counter()
        transformed = apply_operations(
            source,
            operations,
            year=2028,
            planning_date=date(2027, 12, 1),
            scope_orgs=scope,
            org_regions=regions,
            rules=PlanningRules(),
            snapshot_source_batch_id=snapshot.source_batch_id,
        )
        operation_ms = (time.perf_counter() - start_operations) * 1000
        output = calculate_forecast(
            [row.cost for row in transformed],
            policy,
            fx,
            snapshot_id=snapshot.id,
            snapshot_sha256=snapshot.sha256,
            year=2028,
            scope_orgs=scope,
        )
        validate_budget(output, "99999999999999999999")
        elapsed = (time.perf_counter() - begin) * 1000
        digests.add(output["sha256"])
        if iteration >= 3:
            samples["operations_only"].append(operation_ms)
            samples["source_load_operations_and_engine"].append(elapsed)
assert len(digests) == 1, "Repeated calculation drift"
results = {
    kind: {
        "samples": len(values),
        "p50_ms": round(sorted(values)[math.ceil(len(values) * 0.5) - 1], 3),
        "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
        "max_ms": round(max(values), 3),
    }
    for kind, values in samples.items()
}
evidence = {
    "observed_at": datetime.now(UTC).isoformat(),
    "platform": platform.platform(),
    "hardware": "Apple M1, 8 cores, 8 GiB RAM",
    "python": platform.python_version(),
    "deployment": "native local PostgreSQL 17, warm, concurrency 1",
    "warmups": 3,
    "people": snapshot.row_count,
    "source_intervals": len(source),
    "derived_intervals": len(transformed),
    "operations": [op.kind for op in operations],
    "period": 2028,
    "months": 12,
    "snapshot_id": snapshot.id,
    "policy_id": policy.id,
    "fx_set_id": fx.id,
    "planning_rules": PlanningRules().document(),
    "annual": output["annual"],
    "sha256": output["sha256"],
    "results": results,
    "budget_ms": 3000,
    "p95_budget_met": results["source_load_operations_and_engine"]["p95_ms"] < 3000,
    "limits": "Domain + source adapter only. No persisted scenario API, worker, review, browser, hosted or cold timing claim.",
}
target = Path("docs/evidence/p04-operation-performance.json")
target.write_text(json.dumps(evidence, indent=2) + "\n")
print(
    json.dumps({"evidence": str(target), "results": results, "annual": output["annual"]}, indent=2)
)
if not evidence["p95_budget_met"]:
    raise SystemExit("Seven-operation scenario calculation performance budget failed")
