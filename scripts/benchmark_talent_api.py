"""Measured native talent APIs over a real 5k source and current reviewer scopes."""

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
from meridian.identity import digest
from meridian.models import WebSession, now, uid
from meridian.people_models import EmployeeVersion
from meridian.people_seed import CURRENT_SNAPSHOT
from meridian.talent_seed import TALENT_POLICY_ID
from sqlalchemy import select, update
from sqlalchemy.engine import make_url


def main() -> None:
    url = make_url(settings().database_url)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "meridian_demo":
        raise SystemExit("Requires the isolated local demo and running API/worker")
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
            terms=CycleTerms(
                eligibility_date=date(2028, 3, 31),
                merit_effective_date=date(2028, 5, 1),
                bonus_service_start=date(2028, 1, 1),
                bonus_service_end=date(2029, 1, 1),
                bonus_payment_date=date(2029, 1, 15),
            ),
            selected_ids=ids,
        )
        assignments = [
            {
                "employee_id": row.employee_id,
                "author_id": row.proposer_id,
                "calibrator_id": "approver",
                "hr_signer_id": "hr",
            }
            for row in population
            if row.eligible and row.org_id == "D01"
        ]
        assert len(ids) == 5000 and len(assignments) == 414
        for actor, (raw, csrf) in sessions.items():
            db.add(
                WebSession(
                    token_hash=digest(raw),
                    principal_id=actor,
                    csrf_token=csrf,
                    expires_at=now() + timedelta(minutes=10),
                )
            )
    clients = {
        actor: httpx.Client(
            base_url="http://127.0.0.1:8017",
            cookies={"meridian_session": raw},
            headers={"Origin": "http://127.0.0.1:3017", "X-CSRF-Token": csrf},
            timeout=60,
        )
        for actor, (raw, csrf) in sessions.items()
    }
    values: dict[str, list[float]] = {}
    sizes: dict[str, list[int]] = {}
    try:

        def request(actor: str, name: str, method: str, path: str, sample: int, **kwargs):
            began = time.perf_counter()
            response = clients[actor].request(method, path, **kwargs)
            elapsed = (time.perf_counter() - began) * 1000
            response.raise_for_status()
            if sample >= 3:
                values.setdefault(name, []).append(elapsed)
                sizes.setdefault(name, []).append(len(response.content))
            return response.json(), elapsed

        cycle, configure_ms = request(
            "hr",
            "configure",
            "POST",
            "/api/v1/talent/cycles",
            -1,
            headers={"Idempotency-Key": uid()},
            json={
                "title": "Measured human review",
                "snapshot_id": CURRENT_SNAPSHOT,
                "policy_id": TALENT_POLICY_ID,
                "as_of": "2028-03-31",
                "due_date": "2028-06-30",
                "assignments": assignments,
                "reason": "Measure frozen synthetic context with existing reviewer scopes",
            },
        )
        cycle, launch_ms = request(
            "hr",
            "launch",
            "POST",
            f"/api/v1/talent/cycles/{cycle['id']}/launch",
            -1,
            headers={"Idempotency-Key": uid(), "If-Match": str(cycle["version"])},
            json={"reason": "Launch measured synthetic frozen review population"},
        )
        assert cycle["visible_packets"] == 414
        queue_path = f"/api/v1/talent/cycles/{cycle['id']}/packets"
        first, _ = request(
            "manager-a", "first", "GET", queue_path, -1, params={"search": "SYN-000025"}
        )
        packet, _ = request(
            "manager-a", "first", "GET", f"/api/v1/talent/packets/{first['items'][0]['id']}", -1
        )
        for sample in range(23):
            packet, _ = request(
                "manager-a",
                "save_human_author_revision",
                "POST",
                f"/api/v1/talent/packets/{packet['id']}/review",
                sample,
                headers={"Idempotency-Key": uid(), "If-Match": str(packet["version"])},
                json={
                    "review": {
                        "accomplishments": "Synthetic observed demonstration " + str(sample),
                        "collaboration": "Synthetic observed handoff explanation",
                        "development": "Synthetic observed need for stronger citations",
                        "evidence": [
                            {
                                "source_version_id": packet["source_version_id"],
                                "observation": "Synthetic human observation bound to this context",
                            }
                        ],
                    },
                    "reason": "Measure immutable human author revision",
                },
            )
            assert packet["revision"] == sample + 2 and not packet["checklist"]
            _, _ = request(
                "hr",
                "read_cycle_414_packets",
                "GET",
                f"/api/v1/talent/cycles/{cycle['id']}",
                sample,
            )
            page, _ = request(
                "manager-a", "read_queue_25_of_207", "GET", queue_path, sample, params={"limit": 25}
            )
            assert page["total"] == 207 and len(page["items"]) == 25
            _, _ = request(
                "manager-a",
                "read_packet_with_revision_history",
                "GET",
                f"/api/v1/talent/packets/{packet['id']}",
                sample,
            )
            _, _ = request(
                "hr",
                "list_10_review_cycles",
                "GET",
                "/api/v1/talent/cycles",
                sample,
                params={"limit": 10},
            )
        rows = {
            name: {
                "samples": len(samples),
                "p50_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
                "maximum_response_bytes": max(sizes[name]),
            }
            for name, samples in values.items()
        }
        output = {
            "measured_at": now().isoformat(),
            "hardware": {
                "chip": "Apple M1",
                "cpu_cores": 8,
                "ram_gib": 8,
                "os": platform.platform(),
            },
            "scope": "native warm local actual HTTP; no OIDC/hosted/cold or completed 414-person review claim",
            "dataset": {
                "synthetic_people": 5000,
                "source_intervals": 5002,
                "selected_packets": 414,
                "manager_a_packets": 207,
            },
            "concurrency": 1,
            "warmups": 3,
            "measured_samples": 20,
            "cycle_id": cycle["id"],
            "single_configure_ms": round(configure_ms, 3),
            "single_launch_ms": round(launch_ms, 3),
            "results": rows,
            "worker": "independently running; no new job required for an author draft",
            "notes": "23 distinct author revisions persist; scopes unchanged; launch/configuration each one observation",
        }
        Path("docs/evidence/p06-api-performance.json").write_text(
            json.dumps(output, indent=2) + "\n"
        )
        print(json.dumps(rows, indent=2))
    finally:
        for client in clients.values():
            client.close()
        with SessionLocal.begin() as db:
            db.execute(
                update(WebSession).where(WebSession.token_hash.in_(hashes)).values(revoked=True)
            )


if __name__ == "__main__":
    main()
