"""Version-bound, minimal analytical extracts and deterministic private Parquet."""

import calendar
import hashlib
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.compensation_approvals import approvals_for
from meridian.compensation_exports import verified_artifact
from meridian.compensation_models import (
    CompCycle,
    CompDecision,
    CompEligibility,
    CompLock,
    CompPool,
)
from meridian.compensation_service import latest_rows
from meridian.errors import DomainError
from meridian.finance_engine import EXPONENTS
from meridian.finance_repository import exact_decimal
from meridian.ingestion_models import IngestionInput
from meridian.models import OutboxEvent
from meridian.object_store import MAX_UPLOAD_BYTES, put_private
from meridian.people_models import EmployeeVersion, OrgUnit, OrgVersion, Snapshot, SourceBatch
from meridian.policy import canonical_hash
from meridian.scenario_models import Scenario, ScenarioRevision, ScenarioVerification
from meridian.scenario_service import revision_for, stored_forecast

MODEL_VERSION = "meridian-analytics-v1"
ROOT = Path(__file__).resolve().parents[3]

# Every column is internal and allowlisted; no names, base salaries or narratives.
SCHEMAS: dict[str, dict[str, str]] = {
    "workforce": {
        "snapshot_id": "VARCHAR",
        "version_id": "VARCHAR",
        "employee_id": "VARCHAR",
        "position_id": "VARCHAR",
        "org_id": "VARCHAR",
        "manager_id": "VARCHAR",
        "status": "VARCHAR",
        "fte": "DECIMAL(5,4)",
        "valid_from": "DATE",
        "valid_to": "DATE",
        "source_batch_id": "VARCHAR",
        "recorded_at": "TIMESTAMPTZ",
    },
    "orgs": {
        "version_id": "VARCHAR",
        "snapshot_id": "VARCHAR",
        "org_id": "VARCHAR",
        "parent_id": "VARCHAR",
        "name": "VARCHAR",
        "cost_center": "VARCHAR",
        "region": "VARCHAR",
        "valid_from": "DATE",
        "valid_to": "DATE",
    },
    "comp": {
        "cycle_id": "VARCHAR",
        "lock_id": "VARCHAR",
        "event_id": "VARCHAR",
        "proposal_id": "VARCHAR",
        "revision_id": "VARCHAR",
        "employee_id": "VARCHAR",
        "org_id": "VARCHAR",
        "pool_id": "VARCHAR",
        "currency": "VARCHAR",
        "exponent": "INTEGER",
        "annualized_merit_minor": "DECIMAL(38,0)",
        "merit_first_year_minor": "DECIMAL(38,0)",
        "bonus_payable_minor": "DECIMAL(38,0)",
        "pool_charge_minor": "DECIMAL(38,0)",
        "reporting_currency": "VARCHAR",
        "reporting_pool_charge_minor": "DECIMAL(38,0)",
        "guideline_id": "VARCHAR",
        "fx_set_id": "VARCHAR",
        "signature_count": "INTEGER",
    },
    "pools": {
        "cycle_id": "VARCHAR",
        "pool_id": "VARCHAR",
        "org_id": "VARCHAR",
        "currency": "VARCHAR",
        "cost_center": "VARCHAR",
        "reporting_currency": "VARCHAR",
        "budget_minor": "DECIMAL(38,0)",
        "holdback_minor": "DECIMAL(38,0)",
    },
    "decisions": {
        "cycle_id": "VARCHAR",
        "decision_id": "VARCHAR",
        "proposal_id": "VARCHAR",
        "revision_id": "VARCHAR",
        "action": "VARCHAR",
        "ordinal": "INTEGER",
        "actor_id": "VARCHAR",
        "created_at": "TIMESTAMPTZ",
        "correlation_id": "VARCHAR",
    },
    "forecast": {
        "scenario_id": "VARCHAR",
        "revision_id": "VARCHAR",
        "basis_revision_id": "VARCHAR",
        "basis": "VARCHAR",
        "month": "VARCHAR",
        "year": "INTEGER",
        "currency": "VARCHAR",
        "exponent": "INTEGER",
        "base_minor": "DECIMAL(38,0)",
        "benefits_minor": "DECIMAL(38,0)",
        "variable_minor": "DECIMAL(38,0)",
        "employer_tax_minor": "DECIMAL(38,0)",
        "total_minor": "DECIMAL(38,0)",
        "fx_set_id": "VARCHAR",
        "policy_id": "VARCHAR",
        "engine_version": "VARCHAR",
        "model_version": "VARCHAR",
        "result_sha256": "VARCHAR",
    },
    "changes": {
        "scenario_id": "VARCHAR",
        "revision_id": "VARCHAR",
        "operation_id": "VARCHAR",
        "kind": "VARCHAR",
        "effective_date": "DATE",
    },
    "quality": {
        "batch_id": "VARCHAR",
        "state": "VARCHAR",
        "schema_version": "INTEGER",
        "accepted_count": "INTEGER",
        "rejected_count": "INTEGER",
        "watermark": "DATE",
        "published_at": "TIMESTAMPTZ",
    },
    "calendar": {"month_start": "DATE", "month_end": "DATE"},
    "metadata": {
        "snapshot_id": "VARCHAR",
        "source_batch_id": "VARCHAR",
        "watermark": "DATE",
        "data_as_of": "DATE",
        "captured_at": "TIMESTAMPTZ",
        "model_version": "VARCHAR",
    },
}


def model_fingerprint() -> str:
    paths = (
        sorted((ROOT / "analytics/models/governed").rglob("*"))
        + sorted((ROOT / "analytics/macros").rglob("*"))
        + sorted((ROOT / "analytics/tests/governed").rglob("*"))
    )
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(
                str(path.relative_to(ROOT / "analytics")).encode()
                + b"\0"
                + path.read_bytes()
                + b"\0"
            )
    digest.update((ROOT / "analytics/dbt_project.yml").read_bytes())
    return digest.hexdigest()


def normalized(value: Any) -> Any:
    if isinstance(value, Decimal):
        return exact_decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Analytical timestamps require an explicit timezone")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def rows_hash(rows: list[dict[str, Any]]) -> str:
    return canonical_hash(
        [
            {key: normalized(value) for key, value in row.items() if key != "row_sha256"}
            for row in rows
        ]
    )


def parquet_bytes(name: str, rows: list[dict[str, Any]]) -> bytes:
    columns = SCHEMAS[name]
    if any(set(row) != set(columns) for row in rows):
        raise ValueError("Analytical export does not match the observed allowlisted schema")
    with (
        tempfile.TemporaryDirectory(prefix="meridian-parquet-") as directory,
        duckdb.connect() as connection,
    ):
        connection.execute("SET threads=1")
        connection.execute(
            "CREATE TABLE exported ("
            + ",".join(f'"{key}" {kind}' for key, kind in columns.items())
            + ", row_sha256 VARCHAR)"
        )
        if rows:
            connection.executemany(
                "INSERT INTO exported VALUES ("  # noqa: S608 - only static positional placeholders; all row values bound below.
                + ",".join("?" for _ in range(len(columns) + 1))
                + ")",
                [[row[key] for key in columns] + [canonical_hash(row)] for row in rows],
            )
        path = Path(directory) / "export.parquet"
        connection.execute(
            "COPY exported TO ? (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 10000)",
            [str(path)],
        )
        data = path.read_bytes()
    if len(data) > MAX_UPLOAD_BYTES:
        raise DomainError(
            422, "analytics_export_bound", "A private analytical export exceeds the work limit."
        )
    return data


def snapshot_for(db: Session, snapshot_id: str) -> Snapshot:
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.status != "PUBLISHED":
        raise DomainError(
            404, "analytics_source_unavailable", "The pinned published source is unavailable."
        )
    return snapshot


def extract(
    db: Session, snapshot_id: str, as_of: date, cycle_ids: list[str], revision_ids: list[str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    snapshot = snapshot_for(db, snapshot_id)
    data: dict[str, list[dict[str, Any]]] = {name: [] for name in SCHEMAS}
    versions = list(
        db.scalars(
            select(EmployeeVersion)
            .where(EmployeeVersion.snapshot_id == snapshot.id)
            .order_by(EmployeeVersion.id)
            .limit(20001)
        )
    )
    if len(versions) > 20000 or len({row.employee_id for row in versions}) != snapshot.row_count:
        raise DomainError(
            422,
            "analytics_source_bound",
            "The source count or analytical work limit cannot be verified.",
        )
    for row in versions:
        data["workforce"].append(
            {
                "snapshot_id": snapshot.id,
                "version_id": row.id,
                "employee_id": row.employee_id,
                "position_id": row.position_id,
                "org_id": row.org_id,
                "manager_id": row.manager_id,
                "status": row.status,
                "fte": exact_decimal(row.fte),
                "valid_from": row.valid_from.isoformat(),
                "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                "source_batch_id": row.source_batch_id,
                "recorded_at": row.recorded_at.isoformat(),
            }
        )
    orgs = list(
        db.execute(
            select(OrgVersion, OrgUnit.cost_center)
            .join(OrgUnit, OrgUnit.id == OrgVersion.org_id)
            .where(OrgVersion.snapshot_id == snapshot.id)
            .order_by(OrgVersion.id)
        )
    )
    for org_row, center in orgs:
        data["orgs"].append(
            {
                "version_id": org_row.id,
                "snapshot_id": snapshot.id,
                "org_id": org_row.org_id,
                "parent_id": org_row.parent_id,
                "name": org_row.name,
                "cost_center": center,
                "region": org_row.region,
                "valid_from": org_row.valid_from.isoformat(),
                "valid_to": org_row.valid_to.isoformat() if org_row.valid_to else None,
            }
        )
    batch_ids: set[str] = set()
    next_batch: str | None = snapshot.source_batch_id
    while next_batch and next_batch not in batch_ids:
        batch_ids.add(next_batch)
        if len(batch_ids) > 20:
            raise DomainError(
                422,
                "analytics_source_lineage_bound",
                "Source correction history exceeds the work limit.",
            )
        batch = db.get(SourceBatch, next_batch)
        if batch is None:
            raise DomainError(
                503, "analytics_source_lineage_missing", "A pinned source batch is missing."
            )
        data["quality"].append(
            {
                "batch_id": batch.id,
                "state": batch.state,
                "schema_version": batch.schema_version,
                "accepted_count": batch.accepted_count,
                "rejected_count": batch.rejected_count,
                "watermark": batch.watermark.isoformat(),
                "published_at": batch.published_at.isoformat() if batch.published_at else None,
            }
        )
        linked = db.get(IngestionInput, next_batch)
        next_batch = linked.supersedes_batch_id if linked else None
    data["quality"].sort(key=lambda row: row["batch_id"])
    pins: dict[str, Any] = {
        "snapshot_id": snapshot.id,
        "snapshot_sha256": snapshot.sha256,
        "source_batch_id": snapshot.source_batch_id,
        "as_of": as_of.isoformat(),
        "cycles": [],
        "scenarios": [],
    }
    for cycle_id in sorted(cycle_ids):
        cycle = db.get(CompCycle, cycle_id)
        if cycle is None or cycle.snapshot_id != snapshot.id:
            raise DomainError(
                404,
                "analytics_cycle_unavailable",
                "The selected cycle does not belong to this source.",
            )
        artifact = verified_artifact(db, cycle)
        locked = db.get(CompLock, artifact.lock_id)
        event = db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == "comp.cycle_locked.v1",
                OutboxEvent.aggregate_id == cycle.id,
            )
            .order_by(OutboxEvent.occurred_at)
            .limit(1)
        )
        if locked is None or event is None or str(event.payload.get("lock_id")) != locked.id:
            raise DomainError(
                503,
                "analytics_cycle_event_missing",
                "The exact locked-cycle delivery event is unavailable.",
            )
        membership = {
            r.employee_id: r.pool_id
            for r in db.scalars(select(CompEligibility).where(CompEligibility.cycle_id == cycle.id))
        }
        for proposal, revision in latest_rows(db, cycle.id):
            if (
                proposal.state != "LOCKED"
                or len(approvals_for(db, cycle, proposal, revision.id)) != 3
            ):
                raise DomainError(
                    503,
                    "analytics_approval_missing",
                    "A locked award lacks its complete signature route.",
                )
            award = revision.award
            data["comp"].append(
                {
                    "cycle_id": cycle.id,
                    "lock_id": locked.id,
                    "event_id": event.id,
                    "proposal_id": proposal.id,
                    "revision_id": revision.id,
                    "employee_id": proposal.employee_id,
                    "org_id": award["org_id"],
                    "pool_id": membership[proposal.employee_id],
                    "currency": award["currency"],
                    "exponent": EXPONENTS[award["currency"]],
                    **{
                        key: award[key]
                        for key in (
                            "annualized_merit_minor",
                            "merit_first_year_minor",
                            "bonus_payable_minor",
                            "pool_charge_minor",
                            "reporting_currency",
                            "reporting_pool_charge_minor",
                        )
                    },
                    "guideline_id": cycle.guideline_id,
                    "fx_set_id": cycle.fx_set_id,
                    "signature_count": 3,
                }
            )
        proposal_ids = [row[0].id for row in latest_rows(db, cycle.id)]
        for decision in db.scalars(
            select(CompDecision)
            .where(CompDecision.proposal_id.in_(proposal_ids))
            .order_by(CompDecision.id)
        ):
            data["decisions"].append(
                {
                    "cycle_id": cycle.id,
                    "decision_id": decision.id,
                    "proposal_id": decision.proposal_id,
                    "revision_id": decision.revision_id,
                    "action": decision.action,
                    "ordinal": decision.ordinal,
                    "actor_id": decision.actor_id,
                    "created_at": decision.created_at.isoformat(),
                    "correlation_id": decision.request_id,
                }
            )
        for pool in db.scalars(
            select(CompPool).where(CompPool.cycle_id == cycle.id).order_by(CompPool.id)
        ):
            data["pools"].append(
                {
                    "cycle_id": cycle.id,
                    "pool_id": pool.id,
                    "org_id": pool.org_id,
                    "currency": pool.currency,
                    "cost_center": pool.cost_center,
                    "reporting_currency": locked.document["reporting_currency"],
                    "budget_minor": exact_decimal(pool.budget_minor),
                    "holdback_minor": exact_decimal(pool.holdback_minor),
                }
            )
        pins["cycles"].append(
            {
                "cycle_id": cycle.id,
                "lock_id": locked.id,
                "lock_sha256": locked.sha256,
                "export_sha256": artifact.sha256,
                "csv_sha256": artifact.csv_key,
                "event_id": event.id,
                "row_count": artifact.row_count,
                "totals_by_currency": locked.document["totals_by_currency"],
                "reporting_currency": locked.document["reporting_currency"],
                "reporting_pool_charge_minor": locked.document["reporting_pool_charge_minor"],
            }
        )
    for revision_id in sorted(revision_ids):
        scenario_revision = db.get(ScenarioRevision, revision_id)
        scenario = db.get(Scenario, scenario_revision.scenario_id) if scenario_revision else None
        if (
            scenario is None
            or scenario_revision is None
            or scenario.base_snapshot_id != snapshot.id
        ):
            raise DomainError(
                404,
                "analytics_scenario_unavailable",
                "The selected scenario revision does not belong to this source.",
            )
        current = stored_forecast(db, scenario_revision.id)
        baseline = stored_forecast(
            db, scenario.parent_revision_id or revision_for(db, scenario, 1).id
        )
        for stored in (current, baseline):
            verified = db.get(ScenarioVerification, stored.revision_id)
            if verified is None or verified.sha256 != stored.sha256:
                raise DomainError(
                    409,
                    "analytics_forecast_unverified",
                    "Wait for independent verification of the pinned forecast.",
                )
        for basis, stored in (("CURRENT", current), ("BASELINE", baseline)):
            result = stored.document["result"]
            for month in result["months"]:
                data["forecast"].append(
                    {
                        "scenario_id": scenario.id,
                        "revision_id": scenario_revision.id,
                        "basis_revision_id": stored.revision_id,
                        "basis": basis,
                        "month": month["month"],
                        "year": scenario.year,
                        "currency": result["currency"],
                        "exponent": result["exponent"],
                        **{
                            field: month[field]
                            for field in (
                                "base_minor",
                                "benefits_minor",
                                "variable_minor",
                                "employer_tax_minor",
                                "total_minor",
                            )
                        },
                        "fx_set_id": scenario.fx_set_id,
                        "policy_id": scenario.policy_id,
                        "engine_version": result["engine_version"],
                        "model_version": result["model_version"],
                        "result_sha256": result["sha256"],
                    }
                )
        for operation in scenario_revision.operations:
            data["changes"].append(
                {
                    "scenario_id": scenario.id,
                    "revision_id": scenario_revision.id,
                    "operation_id": operation["id"],
                    "kind": operation["kind"],
                    "effective_date": operation["effective_date"],
                }
            )
        pins["scenarios"].append(
            {
                "scenario_id": scenario.id,
                "revision_id": scenario_revision.id,
                "forecast_sha256": current.sha256,
                "baseline_revision_id": baseline.revision_id,
                "baseline_sha256": baseline.sha256,
                "annual_minor": current.document["result"]["annual"]["total_minor"],
                "baseline_minor": baseline.document["result"]["annual"]["total_minor"],
            }
        )
    for month in range(1, 13):
        data["calendar"].append(
            {
                "month_start": date(as_of.year, month, 1).isoformat(),
                "month_end": date(
                    as_of.year, month, calendar.monthrange(as_of.year, month)[1]
                ).isoformat(),
            }
        )
    data["metadata"].append(
        {
            "snapshot_id": snapshot.id,
            "source_batch_id": snapshot.source_batch_id,
            "watermark": snapshot.source_watermark.isoformat(),
            "data_as_of": as_of.isoformat(),
            "captured_at": snapshot.captured_at.isoformat(),
            "model_version": MODEL_VERSION,
        }
    )
    for name in ("comp", "pools", "decisions", "forecast", "changes"):
        data[name].sort(
            key=lambda row: tuple(
                str(row[key]) for key in SCHEMAS[name] if SCHEMAS[name][key] == "VARCHAR"
            )
        )
    active = [
        row
        for row in versions
        if row.status == "ACTIVE"
        and row.valid_from <= as_of
        and (row.valid_to is None or as_of < row.valid_to)
    ]
    pins["expected"] = {
        "headcount": len(active),
        "fte": exact_decimal(sum((row.fte for row in active), Decimal(0))),
        "employee_versions": len(versions),
        "tables": {
            name: {"row_count": len(rows), "rows_sha256": rows_hash(rows)}
            for name, rows in data.items()
        },
    }
    return data, pins


def write_parquet(name: str, rows: list[dict[str, Any]]) -> str:
    return put_private(parquet_bytes(name, rows))
