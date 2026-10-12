"""Reproduce restored persisted financial capsules and private signed evidence."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from operator_db import admin_url
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--data-directory", required=True, type=Path)
    parser.add_argument("--phase", default="p10-restore-replay")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", args.phase):
        raise ValueError("Evidence phase must be a bounded filename identifier")
    if not re.fullmatch(r"meridian_restore_[0-9a-f]{16}", args.database):
        raise ValueError("Only a new isolated restored database may be verified")
    directory = args.data_directory.resolve()
    restore_root = (ROOT / ".local/restores").resolve()
    if (
        args.data_directory.is_symlink()
        or not directory.is_relative_to(restore_root)
        or not directory.is_dir()
    ):
        raise ValueError("Only the private restored object directory may be verified")
    url = make_url(admin_url()).set(database=args.database)
    if url.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Restore verification requires the native project cluster")
    os.environ.update(
        DATABASE_URL=url.render_as_string(hide_password=False),
        LOCAL_DATA_DIR=str(directory),
        APP_ENV="test",
    )
    # Only the isolated restored database is upgraded. Rehearse already verified
    # the archive's counts; forward migration adds current schema/policy metadata
    # so restored runtime accounts can use the current release without owner grants.
    for command in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "meridian.seed"],
    ):
        subprocess.run(
            command, cwd=ROOT, env=os.environ.copy(), check=True, capture_output=True, timeout=60
        )
    from meridian.compensation_models import CompCycle, CompProposal, CompProposalRevision
    from meridian.replay_auth import Subject
    from meridian.replay_models import ReplayAttempt, ReplayCapsule
    from meridian.replay_service import pinned_integrity, reproduce, verified_packet
    from meridian.scenario_models import Scenario, ScenarioRevision

    from scripts.provision_db import provision

    begin = time.perf_counter()
    engine = create_engine(url, hide_parameters=True)
    checks = []
    with Session(engine) as db:
        for kind in ("SCENARIO", "AWARD"):
            query = (
                select(ReplayCapsule, ReplayAttempt)
                .join(ReplayAttempt, ReplayAttempt.capsule_id == ReplayCapsule.id)
                .where(ReplayCapsule.kind == kind, ReplayAttempt.status == "MATCH")
                .order_by(ReplayAttempt.created_at.desc())
                .limit(1)
            )
            if kind == "SCENARIO":
                query = query.where(
                    ReplayCapsule.document["aggregate"]["delta_minor"].as_string() != "0",
                    func.jsonb_array_length(ReplayCapsule.document["decisions"]) > 0,
                )
            pair = db.execute(query).first()
            if pair is None:
                raise ValueError("Restore lacks previously verified financial evidence")
            capsule, attempt = pair
            if kind == "SCENARIO":
                revision = db.get(ScenarioRevision, capsule.subject_id)
                entity = db.get(Scenario, revision.scenario_id) if revision else None
                cycle = None
            else:
                revision = db.get(CompProposalRevision, capsule.subject_id)
                entity = db.get(CompProposal, revision.proposal_id) if revision else None
                cycle = db.get(CompCycle, entity.cycle_id) if entity else None
            if revision is None or entity is None:
                raise ValueError("Restored subject is unavailable")
            subject = Subject(kind, revision, entity, cycle, True)
            issues = pinned_integrity(db, capsule)
            if issues:
                raise ValueError(
                    "Restored source, events or human decisions do not match their fingerprints"
                )
            observed = reproduce(db, capsule, subject)
            raw = verified_packet(db, capsule, attempt, subject)
            if observed != capsule.document["expected_sha256"]:
                raise ValueError("Restored calculation differs from pinned output")
            checks.append(
                {
                    "kind": kind,
                    "capsule_id": capsule.id,
                    "expected_sha256": capsule.document["expected_sha256"],
                    "observed_sha256": observed,
                    "verified_signed_packet_sha256": hashlib.sha256(raw).hexdigest(),
                    "source_lineage_links": len(capsule.document["source_lineage"]),
                    "event_anchors": len(capsule.document["anchors"]),
                    "human_decisions": len(capsule.document["decisions"]),
                    "aggregate": capsule.document["aggregate"],
                }
            )
    engine.dispose()
    passwords = json.loads((ROOT / ".local/runtime-database-credentials.json").read_text())
    provision(url.render_as_string(hide_password=False), passwords)
    report = {
        "scope": "actual isolated native restore, two existing persisted financial capsules; not every historical capsule, hosted PITR, KMS or offsite durability",
        "database": args.database,
        "runtime_grants_reapplied": True,
        "isolated_schema_upgraded_to_current_head": True,
        "checks": checks,
        "seconds": round(time.perf_counter() - begin, 3),
    }
    (ROOT / f"docs/evidence/{args.phase}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
