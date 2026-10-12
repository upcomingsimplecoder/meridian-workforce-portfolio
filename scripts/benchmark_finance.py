"""Measured warm, single-concurrency baseline costing; no API/worker/hosted latency claim."""

import json
import math
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from meridian.db import SessionLocal
from meridian.finance_engine import calculate_forecast
from meridian.finance_repository import baseline_forecast, load_assignments, load_fx, load_policy
from meridian.finance_seed import FX_BASE_ID, POLICY_ID
from meridian.people_seed import CURRENT_SNAPSHOT

SCOPE = tuple(f"D{i:02d}" for i in range(1, 13))


def summarize(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "min_ms": round(ordered[0], 3),
        "p50_ms": round(ordered[math.ceil(len(samples) * 0.5) - 1], 3),
        "p95_ms": round(ordered[math.ceil(len(samples) * 0.95) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }


with SessionLocal() as db:
    snapshot, rows = load_assignments(db, CURRENT_SNAPSHOT)
    policy, fx = load_policy(db, POLICY_ID), load_fx(db, FX_BASE_ID)
    samples: dict[str, list[float]] = {"pure_engine": [], "source_load_plus_engine": []}
    digests: set[str] = set()
    for iteration in range(23):
        for kind in samples:
            start = time.perf_counter()
            if kind == "pure_engine":
                output = calculate_forecast(
                    rows,
                    policy,
                    fx,
                    snapshot_id=snapshot.id,
                    snapshot_sha256=snapshot.sha256,
                    year=2028,
                    scope_orgs=SCOPE,
                )
            else:
                output = baseline_forecast(
                    db,
                    snapshot_id=snapshot.id,
                    policy_id=policy.id,
                    fx_set_id=fx.id,
                    year=2028,
                    scope_orgs=SCOPE,
                )
            elapsed = (time.perf_counter() - start) * 1000
            digests.add(output["sha256"])
            if iteration >= 3:
                samples[kind].append(elapsed)
    assert len(digests) == 1, "Repeated calculation digests differ"
    evidence = {
        "observed_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "hardware": "Apple M1, 8 cores, 8 GiB RAM",
        "deployment": "native local PostgreSQL 17; warm single process, concurrency 1",
        "warmups_per_kind": 3,
        "people": snapshot.row_count,
        "history_intervals": len(rows),
        "months": 12,
        "year": 2028,
        "snapshot_id": snapshot.id,
        "snapshot_sha256": snapshot.sha256,
        "policy_id": policy.id,
        "fx_set_id": fx.id,
        "currency": output["currency"],
        "engine_version": output["engine_version"],
        "annual": output["annual"],
        "result_sha256": output["sha256"],
        "git_base": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "git_worktree_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        ),
        "results": {kind: summarize(values) for kind, values in samples.items()},
        "budget_ms": 3000,
        "p95_budget_met": all(summarize(values)["p95_ms"] < 3000 for values in samples.values()),
        "limits": "Baseline load/calculation only. Scenario operations, request queueing, browser, worker, hosted and cold latency unmeasured.",
    }
    target = Path("docs/evidence/p04-engine-performance.json")
    target.write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {"evidence": str(target), "results": evidence["results"], "annual": output["annual"]},
            indent=2,
        )
    )
    if not evidence["p95_budget_met"]:
        raise SystemExit("Baseline calculation performance gate failed")
