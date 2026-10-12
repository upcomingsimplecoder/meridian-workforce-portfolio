"""Observed local HTTP writes and persisted reads; no hosted or OIDC-latency claim."""

import json
import math
import platform
import secrets
import statistics
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid5

import httpx
from meridian.config import settings
from meridian.db import SessionLocal
from meridian.finance_seed import FX_BASE_ID, POLICY_ID
from meridian.identity import digest
from meridian.models import WebSession, now, uid
from meridian.people_seed import CURRENT_SNAPSHOT
from sqlalchemy import update
from sqlalchemy.engine import make_url

url = make_url(settings().database_url)
if url.host not in {"127.0.0.1", "localhost"} or url.database != "meridian_demo":
    raise SystemExit("Requires isolated local meridian_demo and running API/worker")

raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
token_hash = digest(raw)
with SessionLocal.begin() as db:
    db.add(
        WebSession(
            token_hash=token_hash,
            principal_id="hr",
            csrf_token=csrf,
            expires_at=now() + timedelta(minutes=10),
        )
    )

values: dict[str, list[float]] = {
    "create_5k_baseline": [],
    "save_seven_operations": [],
    "read_persisted_forecast": [],
    "list_10_plans": [],
}
sizes: dict[str, list[int]] = {path: [] for path in values}
observed_totals: set[str] = set()
scenario_ids = []
try:
    with httpx.Client(
        base_url="http://127.0.0.1:8017",
        cookies={"meridian_session": raw},
        headers={"Origin": "http://127.0.0.1:3017", "X-CSRF-Token": csrf},
        timeout=15,
    ) as client:

        def measured(name: str, method: str, path: str, iteration: int, **kwargs):
            start = time.perf_counter()
            response = client.request(method, path, **kwargs)
            elapsed = (time.perf_counter() - start) * 1000
            response.raise_for_status()
            if iteration >= 3:
                values[name].append(elapsed)
                sizes[name].append(len(response.content))
            return response.json()

        for iteration in range(23):
            scenario = measured(
                "create_5k_baseline",
                "POST",
                "/api/v1/scenarios",
                iteration,
                headers={"Idempotency-Key": uid()},
                json={
                    "title": f"Measured workforce plan {iteration}",
                    "base_snapshot_id": CURRENT_SNAPSHOT,
                    "policy_id": POLICY_ID,
                    "fx_set_id": FX_BASE_ID,
                    "reviewer_id": "finance",
                    "year": 2028,
                    "planning_date": "2027-12-01",
                    "scope_orgs": [f"D{i:02d}" for i in range(1, 13)],
                    "budget_minor": "99999999999999999999",
                },
            )
            assert scenario["annual_total_minor"] == "77714704176"
            scenario_ids.append(scenario["id"])
            ns = UUID(uid())
            operations = [
                {
                    "kind": "hire",
                    "employee_id": "SYN-900001",
                    "position_id": "POS-900001",
                    "org_id": "D01",
                    "manager_id": "SYN-000001",
                    "grade": "L1",
                    "annual_pay_minor": "12000000",
                    "currency": "USD",
                    "fte": "0.5000",
                    "effective_date": "2028-02-29",
                },
                {
                    "kind": "transfer",
                    "employee_id": "SYN-000037",
                    "org_id": "D01",
                    "manager_id": "SYN-000001",
                    "effective_date": "2028-03-01",
                },
                {
                    "kind": "vacancy_delay",
                    "hire_operation_id": str(uuid5(ns, "hire")),
                    "effective_date": "2028-03-29",
                },
                {
                    "kind": "benefit_multiplier",
                    "org_id": "D01",
                    "multiplier": "1.28",
                    "effective_date": "2028-04-01",
                },
                {
                    "kind": "merit_assumption",
                    "org_id": "D01",
                    "rate": "0.03",
                    "effective_date": "2028-07-01",
                },
                {
                    "kind": "promotion",
                    "employee_id": "SYN-000049",
                    "grade": "L10",
                    "annual_pay_minor": "24000000",
                    "effective_date": "2028-08-01",
                },
                {"kind": "departure", "employee_id": "SYN-005000", "effective_date": "2028-09-01"},
            ]
            operations = [
                row
                | {
                    "id": str(uuid5(ns, row["kind"])),
                    "reason": "Synthetic measured planning operation",
                }
                for row in operations
            ]
            changed = measured(
                "save_seven_operations",
                "POST",
                f"/api/v1/scenarios/{scenario['id']}/operations",
                iteration,
                headers={"Idempotency-Key": uid(), "If-Match": str(scenario["version"])},
                json={"reason": "Measure all seven validated operations", "operations": operations},
            )
            observed_totals.add(changed["annual_total_minor"])
            assert changed["annual_total_minor"] == "77942031037"
            read = measured(
                "read_persisted_forecast",
                "GET",
                f"/api/v1/scenarios/{scenario['id']}/forecast",
                iteration,
            )
            assert read["current"]["result"]["annual"]["total_minor"] == "77942031037"
            measured("list_10_plans", "GET", "/api/v1/scenarios?limit=10", iteration)
finally:
    with SessionLocal.begin() as db:
        db.execute(
            update(WebSession).where(WebSession.token_hash == token_hash).values(revoked=True)
        )

metrics = [
    {
        "path": name,
        "samples": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
        "max_ms": round(max(samples), 3),
        "response_bytes_max": max(sizes[name]),
    }
    for name, samples in values.items()
]
result = {
    "observed_at": now().isoformat(),
    "hardware": subprocess.check_output(
        ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
    ).strip(),
    "logical_cpus": subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"], text=True).strip(),
    "memory_bytes": subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip(),
    "os": platform.platform(),
    "python": platform.python_version(),
    "people": 5000,
    "source_intervals": 5002,
    "months": 12,
    "conditions": "Loopback HTTP to Uvicorn, native PostgreSQL 17, concurrency 1 request plus independently polling worker, persistent HTTP connection, 3 warmups and 20 measured samples per path. Reference session inserted locally; real OIDC verified separately by Playwright. Warm local only; no cold, browser, external warehouse or hosted latency claim. Synthetic benchmark plans intentionally remain immutable in the demo.",
    "metrics": metrics,
    "observed_annual_total_minor": sorted(observed_totals),
    "persisted_scenario_ids": scenario_ids,
}
Path("docs/evidence/p04-scenario-api-performance.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(metrics, indent=2))
assert all(
    row["p95_ms"]
    < (3000 if row["path"] in {"create_5k_baseline", "save_seven_operations"} else 400)
    for row in metrics
), "Local blueprint performance target exceeded"
