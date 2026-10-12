"""Actual SIGKILL after private export bytes, before SQL acknowledgement; isolated native DB."""

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps/api/tests"))


def exercise(directory):
    from fastapi.testclient import TestClient
    from meridian.compensation_exports import verify_manifest
    from meridian.compensation_models import CompExportArtifact
    from meridian.db import SessionLocal, database_engine
    from meridian.identity import digest
    from meridian.main import app
    from meridian.models import ConsumerReceipt, OutboxEvent, WebSession, now
    from meridian.object_store import ensure_key, read_private
    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from test_compensation_api import locked

    from apps.worker import run as worker

    ensure_key()
    worker.SessionLocal = sessionmaker(
        database_engine(os.environ["WORKER_DATABASE_URL"]), expire_on_commit=False
    )
    with SessionLocal() as db, TestClient(app, base_url="http://127.0.0.1:3017") as client:

        def login(persona="hr"):
            # Test-only trusted sessions in the freshly owned fault-test DB. Real OIDC is
            # separately browser-tested; this drill does not claim new OIDC verification.
            raw = secrets.token_urlsafe(32)
            session = WebSession(
                token_hash=digest(raw),
                principal_id=persona,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now() + timedelta(hours=1),
            )
            db.add(session)
            db.commit()
            client.cookies.set("meridian_session", raw)
            client.headers.update(
                {"Origin": "http://127.0.0.1:3017", "X-CSRF-Token": session.csrf_token}
            )

        def tick():
            result = worker.tick()
            db.expire_all()
            return result

        cycle, _ = locked(client, login, db, tick)
        target = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == cycle["id"],
                OutboxEvent.event_type == "comp.cycle_locked.v1",
            )
        )
        if target is None:
            raise RuntimeError("Persisted lock event missing")
        while (
            db.scalar(
                select(OutboxEvent.id)
                .where(OutboxEvent.state == "PENDING")
                .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
                .limit(1)
            )
            != target.id
        ):
            if tick() != 1:
                raise RuntimeError("Prior actual event did not drain")
        marker = directory / "export-written.json"
        env = os.environ | {
            "DATABASE_URL": os.environ["WORKER_DATABASE_URL"],
            "WORKER_DATABASE_URL": os.environ["WORKER_DATABASE_URL"],
            "MERIDIAN_DRILL_MARKER": str(marker),
            "MERIDIAN_DRILL_CYCLE": cycle["id"],
        }
        with (directory / "child-worker.log").open("wb") as log:
            child = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts/worker_kill_drill.py"), "--child"],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 45
                while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not marker.exists():
                    import shutil

                    shutil.copyfile(
                        directory / "child-worker.log",
                        ROOT / ".local/p10-worker-sigkill-child-failure.log",
                    )
                    raise RuntimeError("Actual post-file barrier not reached")
                written = json.loads(marker.read_text())
                child.send_signal(signal.SIGKILL)
                if child.wait(timeout=10) != -signal.SIGKILL:
                    raise RuntimeError("OS did not report SIGKILL")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
        db.expire_all()
        if (
            db.get(CompExportArtifact, cycle["id"]) is not None
            or db.get(ConsumerReceipt, ("compensation-v1", target.id)) is not None
            or db.get(OutboxEvent, target.id).state != "PENDING"
        ):
            raise RuntimeError("Killed pre-ack transaction incorrectly committed")
        if hashlib.sha256(read_private(written["object_key"])).hexdigest() != written["object_key"]:
            raise RuntimeError("Actual private bytes missing after SIGKILL")
        subprocess.run(
            [sys.executable, "-m", "apps.worker.run", "--once"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            timeout=45,
        )
        db.expire_all()
        artifact = db.get(CompExportArtifact, cycle["id"])
        if (
            artifact is None
            or artifact.sha256 != written["manifest_sha256"]
            or not verify_manifest(artifact.document)
            or not db.get(ConsumerReceipt, ("compensation-v1", target.id))
        ):
            raise RuntimeError("Actual retry failed signature/hash/receipt verification")
        if (
            artifact.document["manifest"]["totals_by_currency"]["USD"]["pool_charge_minor"]
            != "1520000"
        ):
            raise RuntimeError("Compensation golden drift")
        event = db.get(OutboxEvent, target.id)
        event.state = "PENDING"
        db.commit()
        subprocess.run(
            [sys.executable, "-m", "apps.worker.run", "--once"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            timeout=45,
        )
        db.expire_all()
        count = db.scalar(
            select(func.count())
            .select_from(CompExportArtifact)
            .where(CompExportArtifact.cycle_id == cycle["id"])
        )
        receipts = db.scalar(
            select(func.count())
            .select_from(ConsumerReceipt)
            .where(ConsumerReceipt.event_id == target.id)
        )
        if (
            count != 1
            or receipts != 1
            or db.get(CompExportArtifact, cycle["id"]).sha256 != written["manifest_sha256"]
        ):
            raise RuntimeError("Duplicate changed artifact/receipt")
        result = {
            "scope": "Actual isolated native SIGKILL after bytes, pre-ack SQL; non-owner API/worker roles; test-only synthetic sessions; OIDC separately browser-tested",
            "signal": 9,
            "observed_child_exit": -9,
            "after_kill": {
                "artifact_rows": 0,
                "receipt_rows": 0,
                "event_state": "PENDING",
                "bytes_sha256": written["object_key"],
                "manifest_sha256": written["manifest_sha256"],
            },
            "after_retry": {
                "artifact_rows": count,
                "receipt_rows": receipts,
                "signature_valid": True,
                "pool_charge_usd_minor": "1520000",
            },
            "duplicate_delivery": "same single artifact/hash and receipt",
        }
        phase = os.environ.get("EVIDENCE_PHASE", "p10-worker-sigkill")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", phase):
            raise RuntimeError("Invalid drill evidence phase")
        (ROOT / f"docs/evidence/{phase}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    worker.SessionLocal.kw["bind"].dispose()


def child():
    from meridian.compensation_models import CompExportArtifact

    from apps.worker import run as worker

    original = worker.write_export

    def barrier(db, *args, **kwargs):
        original(db, *args, **kwargs)
        db.flush()
        artifact = db.get(CompExportArtifact, os.environ["MERIDIAN_DRILL_CYCLE"])
        if artifact is None:
            raise RuntimeError("Actual export SQL not reached")
        marker = Path(os.environ["MERIDIAN_DRILL_MARKER"])
        marker.write_text(
            json.dumps({"manifest_sha256": artifact.sha256, "object_key": artifact.csv_key})
        )
        marker.chmod(0o600)
        os.kill(os.getpid(), signal.SIGSTOP)
        raise RuntimeError("Unexpected resume of killed worker")

    worker.write_export = barrier
    sys.argv = [sys.argv[0], "--once"]
    worker.main()


def main():
    from scripts.operator_db import admin_url
    from scripts.provision_db import provision

    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--exercise", type=Path)
    args = parser.parse_args()
    if args.child or args.exercise:
        url = make_url(os.environ["DATABASE_URL"])
        if url.host not in {"127.0.0.1", "localhost"} or not url.database.startswith(
            "meridian_kill_"
        ):
            raise RuntimeError("Only a fresh isolated native drill DB is permitted")
        if args.child:
            child()
        else:
            exercise(args.exercise)
        return
    operator = make_url(admin_url())
    if operator.host not in {"127.0.0.1", "localhost"} or operator.database != "meridian_demo":
        raise RuntimeError("Project native operator required")
    name = "meridian_kill_" + uuid4().hex[:16]
    admin = operator.set(database=name).render_as_string(hide_password=False)
    owner_uri = operator.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(owner_uri, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with tempfile.TemporaryDirectory(prefix="worker-kill-", dir=ROOT / ".local") as directory:
            env = os.environ | {
                "APP_ENV": "test",
                "LOCAL_DATA_DIR": directory,
                "DATABASE_URL": admin,
            }
            for command in (
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                [sys.executable, "-m", "meridian.seed"],
            ):
                subprocess.run(
                    command, cwd=ROOT, env=env, check=True, capture_output=True, timeout=60
                )
            passwords = json.loads((ROOT / ".local/runtime-database-credentials.json").read_text())
            provision(admin, passwords)
            env |= {
                "DATABASE_URL": operator.set(
                    database=name, username="meridian_app", password=passwords["meridian_app"]
                ).render_as_string(hide_password=False),
                "WORKER_DATABASE_URL": operator.set(
                    database=name, username="meridian_worker", password=passwords["meridian_worker"]
                ).render_as_string(hide_password=False),
            }
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/worker_kill_drill.py"),
                    "--exercise",
                    directory,
                ],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=120,
            )
    finally:
        with psycopg.connect(owner_uri, autocommit=True) as owner:
            owner.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
