"""Measure real worker claims/receipts on 100k synthetic events in a fresh isolated DB."""

import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from operator_db import admin_url
from psycopg import sql
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    operator = make_url(admin_url())
    if operator.host not in {"127.0.0.1", "localhost"} or operator.database != "meridian_demo":
        raise ValueError("This native benchmark requires the project-owned loopback operator")
    database = "meridian_load_" + uuid4().hex[:16]
    uri = operator.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(uri, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    engine = None
    try:
        with tempfile.TemporaryDirectory(prefix="outbox-load-", dir=ROOT / ".local") as directory:
            admin = operator.set(database=database).render_as_string(hide_password=False)
            env = os.environ | {
                "DATABASE_URL": admin,
                "APP_ENV": "test",
                "LOCAL_DATA_DIR": directory,
            }
            for command in (
                [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
                [str(ROOT / ".venv/bin/python"), "-m", "meridian.seed"],
            ):
                subprocess.run(command, cwd=ROOT, env=env, check=True, capture_output=True)
            from scripts.provision_db import provision

            passwords = json.loads((ROOT / ".local/runtime-database-credentials.json").read_text())
            provision(admin, passwords)
            os.environ.update(env)
            from meridian.models import ConsumerReceipt, OutboxEvent, now, uid
            from meridian.people_models import Employee

            owner_engine = create_engine(admin, hide_parameters=True)
            started = now() - timedelta(seconds=5)
            with owner_engine.begin() as db:
                for _batch in range(100):
                    db.execute(
                        OutboxEvent.__table__.insert(),
                        [
                            {
                                "id": uid(),
                                "event_type": "identity.logout.v1",
                                "schema_version": 1,
                                "aggregate_id": "hr",
                                "aggregate_version": 1,
                                "payload": {"entity_type": "principal", "entity_id": "hr"},
                                "request_id": uid(),
                                "occurred_at": started,
                                "state": "PENDING",
                                "attempts": 0,
                                "next_attempt_at": started,
                            }
                            for _ in range(1000)
                        ],
                    )
            worker_url = operator.set(
                database=database, username="meridian_worker", password=passwords["meridian_worker"]
            ).render_as_string(hide_password=False)
            engine = create_engine(worker_url, pool_pre_ping=True, hide_parameters=True)
            factory = sessionmaker(engine, expire_on_commit=False)
            from apps.worker import run as worker

            worker.SessionLocal = factory
            with factory() as db:
                if (
                    db.scalar(select(func.count()).select_from(OutboxEvent)) != 100000
                    or db.scalar(select(func.count()).select_from(Employee)) != 5000
                ):
                    raise RuntimeError(
                        "Observed benchmark dataset does not match its declared size"
                    )
            samples = []
            for number in range(103):
                begin = time.perf_counter()
                if worker.tick() != 1:
                    raise RuntimeError("Actual worker failed to consume a ready event")
                duration = (time.perf_counter() - begin) * 1000
                if number >= 3:
                    samples.append(duration)
            with factory.begin() as db:
                receipts = db.scalar(select(func.count()).select_from(ConsumerReceipt))
                event = db.scalar(
                    select(OutboxEvent)
                    .where(OutboxEvent.state == "DONE")
                    .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
                    .limit(1)
                )
                if receipts != 103 or event is None:
                    raise RuntimeError("Worker receipts do not match actual completed deliveries")
                event.state = "PENDING"
            if worker.tick() != 1:
                raise RuntimeError("Duplicate delivery was not safely acknowledged")
            with factory() as db:
                after = db.scalar(select(func.count()).select_from(ConsumerReceipt))
                pending = db.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.state == "PENDING")
                )
                if after != receipts or pending != 99897:
                    raise RuntimeError("Duplicate delivery changed the receipt count")
            owner_engine.dispose()
            ordered = sorted(samples)
            report = {
                "observed_at": now().isoformat(),
                "scope": "native standalone worker, real non-owner login and PostgreSQL commits; identity event acknowledgement only; no financial/export/cloud throughput claim",
                "hardware": {
                    "platform": platform.platform(),
                    "cpu_cores": os.cpu_count(),
                    "machine": platform.machine(),
                },
                "dataset": {"employees": 5000, "events": 100000},
                "concurrency": 1,
                "warmups": 3,
                "measured": 100,
                "errors": 0,
                "duplicate_delivery": {
                    "receipts_before": receipts,
                    "receipts_after": after,
                    "pending_after": pending,
                },
                "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 3),
                "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
                "maximum_ms": round(max(samples), 3),
                "samples_ms": [round(value, 3) for value in samples],
            }
            (ROOT / "docs/evidence/p10-outbox-load.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps({key: value for key, value in report.items() if key != "samples_ms"}))
    finally:
        if engine is not None:
            engine.dispose()
        with psycopg.connect(uri, autocommit=True) as owner:
            owner.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


if __name__ == "__main__":
    main()
