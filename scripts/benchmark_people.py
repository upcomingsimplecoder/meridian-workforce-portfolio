"""Observe real authenticated API reads on the synthetic demo. Never benchmark an external host."""

import json
import math
import platform
import secrets
import statistics
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import httpx
from meridian.config import settings
from meridian.db import SessionLocal
from meridian.identity import digest
from meridian.models import WebSession, now
from meridian.people_seed import CURRENT_SNAPSHOT
from sqlalchemy import update
from sqlalchemy.engine import make_url

url = make_url(settings().database_url)
if url.host not in {"127.0.0.1", "localhost"} or url.database != "meridian_demo":
    raise SystemExit("Benchmark requires local meridian_demo")
measurements = []
for persona, endpoint, expected in [
    ("hr", "/api/v1/people?limit=25", 5000),
    ("manager-a", "/api/v1/people?limit=25", 207),
    ("manager-b", "/api/v1/people?limit=25", 625),
    ("hr", "/api/v1/people/SYN-000037/history", None),
    ("executive", "/api/v1/workforce/summary", 5000),
]:
    raw = secrets.token_urlsafe(32)
    token_hash = digest(raw)
    with SessionLocal.begin() as db:
        db.add(
            WebSession(
                token_hash=token_hash,
                principal_id=persona,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now() + timedelta(minutes=5),
            )
        )
    values = []
    sizes = []
    try:
        with httpx.Client(
            base_url="http://127.0.0.1:8017", cookies={"meridian_session": raw}, timeout=10
        ) as client:
            query = (
                endpoint
                + ("&" if "?" in endpoint else "?")
                + f"snapshot_id={CURRENT_SNAPSHOT}&as_of=2027-06-30"
            )
            for attempt in range(105):
                started = time.perf_counter()
                response = client.get(query)
                duration = (time.perf_counter() - started) * 1000
                response.raise_for_status()
                payload = response.json()
                if expected is not None:
                    assert payload.get("total", payload.get("headcount")) == expected
                else:
                    assert len(payload["items"]) == 2
                if attempt >= 5:
                    values.append(duration)
                    sizes.append(len(response.content))
        measurements.append(
            {
                "persona": persona,
                "endpoint": endpoint,
                "requests": len(values),
                "median_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
                "max_ms": round(max(values), 3),
                "response_bytes_max": max(sizes),
            }
        )
    finally:
        with SessionLocal.begin() as db:
            db.execute(
                update(WebSession).where(WebSession.token_hash == token_hash).values(revoked=True)
            )
result = {
    "observed_at": now().isoformat(),
    "hardware": subprocess.check_output(
        ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
    ).strip(),
    "logical_cpus": subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"], text=True).strip(),
    "memory_bytes": subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip(),
    "os": platform.platform(),
    "python": platform.python_version(),
    "employees": 5000,
    "employee_versions": 10002,
    "conditions": "Loopback HTTP directly to Uvicorn, native PostgreSQL 17, persistent connection, concurrency 1, 5 warmups then 100 measured reads per endpoint. Reference benchmark sessions inserted locally; real OIDC is verified by Playwright. No cold-cache or hosted claim.",
    "metrics": measurements,
}
Path("docs/evidence/p02-performance.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(measurements, indent=2))
assert all(row["p95_ms"] < 400 for row in measurements), "Read p95 exceeded blueprint local target"
