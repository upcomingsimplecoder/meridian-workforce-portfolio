"""Real local HTTP/worker measurements; existing grants, no OIDC or hosted latency claim."""

import argparse
import json
import math
import platform
import secrets
import statistics
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
from meridian.compensation_engine import CycleTerms
from meridian.compensation_repository import load_eligibility
from meridian.compensation_seed import GUIDELINE_ID
from meridian.config import settings
from meridian.db import SessionLocal
from meridian.finance_seed import FX_BASE_ID
from meridian.identity import digest
from meridian.models import WebSession, now, uid
from meridian.people_models import EmployeeVersion
from meridian.people_seed import CURRENT_SNAPSHOT
from sqlalchemy import select, update
from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-cycle")
    args = parser.parse_args()
    url = make_url(settings().database_url)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "meridian_demo":
        raise SystemExit("Requires isolated local demo and running API/worker")
    terms = CycleTerms(
        eligibility_date=date(2028, 3, 31),
        merit_effective_date=date(2028, 5, 1),
        bonus_service_start=date(2028, 1, 1),
        bonus_service_end=date(2029, 1, 1),
        bonus_payment_date=date(2029, 1, 15),
    )
    sessions = {
        actor: (secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        for actor in ("hr", "manager-a")
    }
    hashes = [digest(raw) for raw, _ in sessions.values()]
    with SessionLocal.begin() as db:
        ids = tuple(
            db.scalars(
                select(EmployeeVersion.employee_id)
                .where(EmployeeVersion.snapshot_id == CURRENT_SNAPSHOT)
                .distinct()
                .order_by(EmployeeVersion.employee_id)
            )
        )
        population = load_eligibility(
            db,
            snapshot_id=CURRENT_SNAPSHOT,
            guideline_id=GUIDELINE_ID,
            terms=terms,
            selected_ids=ids,
        )
        # D02 has no separately assigned division reviewer in this fixture. Preserve
        # D01's reviewer scope; do not silently grant broader benchmark authority.
        selected = [
            row.employee_id for row in population if not row.eligible or row.org_id == "D01"
        ]
        assert len(ids) == 5000 and len(selected) == 4582
        assert sum(row.eligible and row.org_id == "D01" for row in population) == 414
        for actor, (raw, csrf) in sessions.items():
            db.add(
                WebSession(
                    token_hash=digest(raw),
                    principal_id=actor,
                    csrf_token=csrf,
                    expires_at=now() + timedelta(minutes=10),
                )
            )
    values, sizes = {}, {}
    clients = {
        actor: httpx.Client(
            base_url="http://127.0.0.1:8017",
            cookies={"meridian_session": raw},
            headers={"Origin": "http://127.0.0.1:3017", "X-CSRF-Token": csrf},
            timeout=60,
        )
        for actor, (raw, csrf) in sessions.items()
    }
    try:

        def measured(actor, name, method, path, iteration, **kwargs):
            started = time.perf_counter()
            response = clients[actor].request(method, path, **kwargs)
            elapsed = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            if iteration >= 3:
                values.setdefault(name, []).append(elapsed)
                sizes.setdefault(name, []).append(len(response.content))
            return response.json()

        if args.reuse_cycle:
            response = clients["hr"].get(f"/api/v1/compensation/cycles/{args.reuse_cycle}")
            response.raise_for_status()
            cycle = response.json()
            assert cycle["snapshot_id"] == CURRENT_SNAPSHOT and cycle["state"] == "OPEN"
            launch_ms = None
        else:
            for sample in range(23):
                cycle = measured(
                    "hr",
                    "configure_4582_selected_from_5k",
                    "POST",
                    "/api/v1/compensation/cycles",
                    sample,
                    headers={"Idempotency-Key": uid()},
                    json={
                        "title": f"Measured department review {sample}",
                        "snapshot_id": CURRENT_SNAPSHOT,
                        "guideline_id": GUIDELINE_ID,
                        "fx_set_id": FX_BASE_ID,
                        "selected_ids": selected,
                        "terms": terms.document(),
                        "pools": [
                            {
                                "manager_id": actor,
                                "org_id": "D01",
                                "currency": "USD",
                                "budget_minor": "99999999999999999999",
                                "holdback_minor": "0",
                            }
                            for actor in ("manager-a", "manager-b")
                        ],
                        "approvals": [
                            {
                                "org_id": "D01",
                                "division_approver_id": "approver",
                                "hr_signoff_id": "hr",
                                "finance_signoff_id": "finance",
                            }
                        ],
                        "reason": "Measured synthetic scoped cycle configuration",
                    },
                )
            started = time.perf_counter()
            response = clients["hr"].post(
                f"/api/v1/compensation/cycles/{cycle['id']}/launch",
                headers={"Idempotency-Key": uid(), "If-Match": str(cycle["version"])},
                json={"reason": "Measured frozen eligibility and initial zero-award launch"},
            )
            launch_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            cycle = response.json()
            assert cycle["visible_proposals"] == 414 and cycle["ineligible_count"] == 4168
        proposals_path = f"/api/v1/compensation/cycles/{cycle['id']}/proposals"
        first = clients["manager-a"].get(proposals_path, params={"search": "SYN-000025"})
        first.raise_for_status()
        proposal = first.json()["items"][0]
        for sample in range(23):
            proposal = measured(
                "manager-a",
                "save_exact_manager_award",
                "POST",
                f"/api/v1/compensation/proposals/{proposal['id']}/award",
                sample,
                headers={"Idempotency-Key": uid(), "If-Match": str(proposal["version"])},
                json={
                    "merit_minor": "369000",
                    "bonus_minor": "984000",
                    "comment": "Measured synthetic 3% merit and 8% full-service bonus",
                    "reason": "Measured exact manager award save",
                },
            )
            for field, expected in {
                "annualized_merit_minor": "369000",
                "merit_first_year_minor": "246000",
                "bonus_payable_minor": "984000",
                "pool_charge_minor": "1353000",
            }.items():
                assert proposal["award"][field] == expected
            measured(
                "hr",
                "read_cycle_414_proposals",
                "GET",
                f"/api/v1/compensation/cycles/{cycle['id']}",
                sample,
            )
            page = measured(
                "manager-a",
                "read_scoped_roster_25_of_207",
                "GET",
                proposals_path,
                sample,
                params={"limit": 25},
            )
            assert page["total"] == 207 and len(page["items"]) == 25
            measured(
                "hr",
                "list_10_cycles",
                "GET",
                "/api/v1/compensation/cycles",
                sample,
                params={"limit": 10},
            )
        began = time.perf_counter()
        while time.perf_counter() - began < 90:
            checked = clients["manager-a"].get(f"/api/v1/compensation/proposals/{proposal['id']}")
            checked.raise_for_status()
            if checked.json()["verified"]:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Actual latest-revision worker verification timed out")
        metrics = {
            name: {
                "samples": len(samples),
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[math.ceil(0.95 * len(samples)) - 1], 3),
                "max_response_bytes": max(sizes[name]),
            }
            for name, samples in values.items()
        }
        document = {
            "claim_scope": "warm local HTTP concurrency 1 with independent worker; trusted local sessions use existing role grants; OIDC login, completed 414-person review, cold/hosted/warehouse throughput unmeasured",
            "hardware": {
                "processor": "Apple M1",
                "cpu_cores": 8,
                "ram_gib": 8,
                "os": platform.platform(),
                "postgresql_major": 17,
            },
            "dataset": {
                "snapshot_id": CURRENT_SNAPSHOT,
                "source_people": 5000,
                "source_intervals": 5002,
                "selected_people": 4582,
                "frozen_eligible_d01": 414,
                "explicit_ineligible": 4168,
                "outside_selected_review_department": 418,
            },
            "reused_existing_cycle": bool(args.reuse_cycle),
            "warmups_per_path": 3,
            "samples_per_path": 20,
            "metrics": metrics,
            "launch_single_observation_ms": round(launch_ms, 3) if launch_ms is not None else None,
            "latest_award_worker_verified": True,
            "latest_worker_wait_ms": round((time.perf_counter() - began) * 1000, 3),
            "observed_single_award": {
                field: proposal["award"][field]
                for field in (
                    "annualized_merit_minor",
                    "merit_first_year_minor",
                    "bonus_payable_minor",
                    "pool_charge_minor",
                )
            },
            "cycle_id": cycle["id"],
        }
        Path("docs/evidence/p05-api-performance.json").write_text(
            json.dumps(document, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    "metrics": metrics,
                    "launch_single_observation_ms": document["launch_single_observation_ms"],
                    "latest_award_worker_verified": True,
                },
                indent=2,
            )
        )
    finally:
        for client in clients.values():
            client.close()
        with SessionLocal.begin() as db:
            db.execute(
                update(WebSession).where(WebSession.token_hash.in_(hashes)).values(revoked=True)
            )


if __name__ == "__main__":
    main()
