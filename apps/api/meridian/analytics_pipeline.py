"""Real isolated DuckDB/dbt runs; publish only count/sum/hash reconciled results."""

import gc
import hashlib
import hmac
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.analytics_exports import (
    MODEL_VERSION,
    ROOT,
    SCHEMAS,
    extract,
    model_fingerprint,
    normalized,
    rows_hash,
    write_parquet,
)
from meridian.analytics_models import (
    AnalyticsArtifact,
    AnalyticsDelivery,
    AnalyticsPublication,
    AnalyticsResult,
    AnalyticsRun,
)
from meridian.config import settings
from meridian.errors import DomainError
from meridian.governance import record
from meridian.object_store import materialize_private, put_private, read_private, signing_key
from meridian.policy import canonical_hash

MAX_CATALOG_BYTES = 32 * 1024 * 1024
SIGNING_CONTEXT = b"meridian.analytics.manifest.v1\0"
FACTS = {
    "workforce": "dim_employee_scd2",
    "orgs": "dim_org_scd2",
    "comp": "fct_comp_proposal_revision",
    "pools": "stg_pools",
    "decisions": "fct_approval_decision",
    "forecast": "fct_workforce_forecast_month",
    "changes": "fct_scenario_change",
    "quality": "stg_quality",
    "calendar": "stg_calendar",
    "metadata": "stg_metadata",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def signed_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    key = signing_key()
    return {
        "manifest": manifest,
        "signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "digest": hmac.new(
                key, SIGNING_CONTEXT + canonical_bytes(manifest), hashlib.sha256
            ).hexdigest(),
            "scope": "local analytical artifact authenticity; not external attestation",
        },
    }


def signature_valid(document: dict[str, Any]) -> bool:
    if set(document) != {"manifest", "signature"}:
        return False
    expected = signed_manifest(document["manifest"])
    actual = document.get("signature", {})
    return (
        isinstance(actual.get("digest"), str)
        and hmac.compare_digest(actual["digest"], expected["signature"]["digest"])
        and actual == expected["signature"]
    )


def sql_rows(connection: Any, table: str, columns: list[str] | None = None) -> list[dict[str, Any]]:
    # Callers supply only the static internal allowlist, never request text.
    allowed = set(FACTS.values()) | {
        "mart_workforce_kpis",
        "mart_source_reconciliation",
        "mart_comp_cycle_budget",
        "mart_scenario_delta",
        "mart_workflow_sla",
        "mart_ingestion_quality",
        "fct_headcount_month",
    }
    if table not in allowed:
        raise ValueError("Unknown curated analytical relation")
    schema_for_table = {value: SCHEMAS[name] for name, value in FACTS.items()}
    if columns is not None and (
        not columns
        or table not in schema_for_table
        or any(
            not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", column)
            or column not in schema_for_table[table]
            for column in columns
        )
    ):
        raise ValueError("Unknown curated analytical projection")
    projection = ",".join('"' + column + '"' for column in columns) if columns else "*"
    cursor = connection.execute(f'SELECT {projection} FROM "{table}"')  # noqa: S608 - relation and projected columns explicitly allowlisted above.
    names = [item[0] for item in cursor.description]
    return [
        {key: normalized(value) for key, value in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


def sorted_fact(name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        ["version_id"]
        if name in {"workforce", "orgs"}
        else ["batch_id"]
        if name == "quality"
        else ["month_start"]
        if name == "calendar"
        else [key for key in SCHEMAS[name] if SCHEMAS[name][key] == "VARCHAR"]
    )
    return sorted(rows, key=lambda row: tuple(str(row[key]) for key in keys))


def build_dbt(directory: Path, export_dir: Path, run_id: str) -> Path:
    catalog = directory / "catalog.duckdb"
    catalog.unlink(missing_ok=True)
    catalog.with_suffix(".duckdb.wal").unlink(missing_ok=True)
    env = os.environ | {
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "MERIDIAN_ANALYTICS_PATH": str(catalog),
        "MERIDIAN_DBT_THREADS": "1",
    }
    variables = json.dumps({"business_run": True, "export_dir": str(export_dir), "run_id": run_id})
    command = [
        str(ROOT / ".venv/bin/dbt"),
        "--log-path",
        str(directory / "logs"),
        "build",
        "--project-dir",
        str(ROOT / "analytics"),
        "--profiles-dir",
        str(ROOT / "analytics"),
        "--target-path",
        str(directory / "dbt-target"),
        "--select",
        "tag:governed",
        "--vars",
        variables,
    ]
    # The executable and flags are fixed; data is a single JSON argument and no shell runs.
    completed = subprocess.run(  # noqa: S603
        command,
        env=env,
        capture_output=True,
        timeout=settings().analytics_dbt_timeout_seconds,
    )
    (directory / "dbt-build.log").write_bytes(completed.stdout + completed.stderr)
    (directory / "dbt-build.log").chmod(0o600)
    if completed.returncode:
        raise RuntimeError("ANALYTICS_MODELS_FAILED")
    results = json.loads((directory / "dbt-target/run_results.json").read_text())
    if not results["results"] or any(
        row["status"] not in {"success", "pass"} for row in results["results"]
    ):
        raise RuntimeError("ANALYTICS_MODELS_NOT_VERIFIED")
    catalog.chmod(0o600)
    return catalog


def compare_report(connection: Any, run: AnalyticsRun) -> dict[str, Any]:
    comparisons: list[dict[str, Any]] = []

    def compare(dataset: str, key: str, field: str, expected: Any, observed: Any) -> None:
        expected, observed = str(expected), str(observed)
        comparisons.append(
            {
                "dataset": dataset,
                "key": key,
                "field": field,
                "expected": expected,
                "observed": observed,
                "matches": expected == observed,
            }
        )

    for name, table in FACTS.items():
        rows = sorted_fact(name, sql_rows(connection, table, list(SCHEMAS[name])))
        expected = run.inputs["expected"]["tables"][name]
        compare(name, name, "row_count", expected["row_count"], len(rows))
        compare(name, name, "rows_sha256", expected["rows_sha256"], rows_hash(rows))
    kpis = sql_rows(connection, "mart_workforce_kpis")
    whole = next(row for row in kpis if row["org_id"] == "ALL")
    source = sql_rows(connection, "mart_source_reconciliation")[0]
    for field in ("headcount", "fte"):
        compare("workforce", run.snapshot_id, field, run.inputs["expected"][field], whole[field])
    compare(
        "workforce",
        run.snapshot_id,
        "employee_versions",
        run.inputs["expected"]["employee_versions"],
        source["employee_versions"],
    )
    compensation = sorted(
        sql_rows(connection, "mart_comp_cycle_budget"),
        key=lambda row: (row["cycle_id"], row["currency"]),
    )
    for pinned in run.inputs["cycles"]:
        rows = [row for row in compensation if row["cycle_id"] == pinned["cycle_id"]]
        compare(
            "compensation",
            pinned["cycle_id"],
            "row_count",
            pinned["row_count"],
            sum(row["row_count"] for row in rows),
        )
        compare(
            "compensation",
            pinned["cycle_id"],
            "reporting_pool_charge_minor",
            pinned["reporting_pool_charge_minor"],
            sum(int(row["reporting_pool_charge_minor"]) for row in rows),
        )
        for currency, values in pinned["totals_by_currency"].items():
            actual = next((row for row in rows if row["currency"] == currency), {})
            for field, expected in values.items():
                compare(
                    "compensation",
                    pinned["cycle_id"] + ":" + currency,
                    field,
                    expected,
                    actual.get(field, "MISSING"),
                )
    scenarios = sorted(
        sql_rows(connection, "mart_scenario_delta"), key=lambda row: row["revision_id"]
    )
    for pinned in run.inputs["scenarios"]:
        actual = next((row for row in scenarios if row["revision_id"] == pinned["revision_id"]), {})
        for field in ("annual_minor", "baseline_minor"):
            compare(
                "scenario",
                pinned["revision_id"],
                field,
                pinned[field],
                actual.get(field, "MISSING"),
            )
        compare(
            "scenario",
            pinned["revision_id"],
            "delta_minor",
            int(pinned["annual_minor"]) - int(pinned["baseline_minor"]),
            actual.get("delta_minor", "MISSING"),
        )
    return {
        "model_version": run.model_version,
        "model_sha256": run.model_sha256,
        "inputs_sha256": run.inputs_sha256,
        "headcount": whole["headcount"],
        "fte": whole["fte"],
        "employee_versions": source["employee_versions"],
        "compensation": compensation,
        "scenarios": scenarios,
        "comparisons": comparisons,
        "workflow": sorted(
            sql_rows(connection, "mart_workflow_sla"), key=lambda row: row["cycle_id"]
        ),
        "ingestion": sorted(
            sql_rows(connection, "mart_ingestion_quality"), key=lambda row: row["batch_id"]
        ),
    }


def execute_run(
    db: Session, run: AnalyticsRun, *, preserve: Sequence[Any] = ()
) -> AnalyticsResult:
    existing = db.get(AnalyticsResult, run.id)
    if existing:
        return existing
    if (
        run.inputs_sha256 != canonical_hash(run.inputs)
        or run.model_version != MODEL_VERSION
        or run.model_sha256 != model_fingerprint()
    ):
        raise RuntimeError("ANALYTICS_PINNED_MODEL_OR_INPUTS_CHANGED")
    data, inputs = extract(
        db,
        run.snapshot_id,
        run.as_of,
        [row["cycle_id"] for row in run.inputs["cycles"]],
        [row["revision_id"] for row in run.inputs["scenarios"]],
    )
    if inputs != run.inputs:
        raise RuntimeError("ANALYTICS_PINNED_SOURCE_CHANGED")
    # The extract uses ORM rows for lineage and authorization checks. Once the
    # scalar, allowlisted export is ready, release those objects before the
    # DuckDB/dbt child starts. This keeps the hosted free-tier process below
    # its memory limit without changing the pinned inputs.
    db.expunge_all()
    db.add(run)
    # The outbox consumer keeps its locked event in this same transaction. It
    # must remain attached so the consumer can persist DONE or retry state
    # after this memory release step.
    for item in preserve:
        db.add(item)
    directory = Path(settings().local_data_dir).resolve() / "analytics/runs" / str(UUID(run.id))
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    export_dir = directory / "loaded"
    export_dir.mkdir(mode=0o700, exist_ok=True)
    loaded: dict[str, Any] = {}
    dropped_cycles = {
        row["cycle_id"] for row in inputs["cycles"] if row["event_id"] == run.dropped_event_id
    }
    for name, rows in data.items():
        key = write_parquet(name, rows)
        db.add(
            AnalyticsArtifact(
                run_id=run.id,
                name=name,
                storage_key=key,
                rows_sha256=rows_hash(rows),
                row_count=len(rows),
            )
        )
        projected = (
            [row for row in rows if row.get("cycle_id") not in dropped_cycles]
            if name in {"comp", "decisions"}
            else rows
        )
        loaded_key = key if projected == rows else write_parquet(name, projected)
        path = export_dir / (name + ".parquet")
        path.write_bytes(read_private(loaded_key))
        path.chmod(0o600)
        loaded[name] = {
            "sha256": loaded_key,
            "row_count": len(projected),
            "rows_sha256": rows_hash(projected),
        }
    db.flush()
    # Source delivery is explicit even for the original seed, which has no fabricated domain event.
    db.add(
        AnalyticsDelivery(
            run_id=run.id,
            delivery_key="snapshot:" + run.snapshot_id,
            artifact_sha256=inputs["expected"]["tables"]["workforce"]["rows_sha256"],
            row_count=len(data["workforce"]),
        )
    )
    for cycle in inputs["cycles"]:
        if cycle["event_id"] != run.dropped_event_id:
            rows = [row for row in data["comp"] if row["cycle_id"] == cycle["cycle_id"]]
            db.add(
                AnalyticsDelivery(
                    run_id=run.id,
                    delivery_key=cycle["event_id"],
                    artifact_sha256=rows_hash(rows),
                    row_count=len(rows),
                )
            )
    for scenario in inputs["scenarios"]:
        rows = [row for row in data["forecast"] if row["revision_id"] == scenario["revision_id"]]
        db.add(
            AnalyticsDelivery(
                run_id=run.id,
                delivery_key="scenario:" + scenario["revision_id"],
                artifact_sha256=rows_hash(rows),
                row_count=len(rows),
            )
        )
    # Artifacts and delivery intents are now flushed to the transaction. Do not
    # retain every extracted row while dbt materializes its derived catalog.
    del data
    gc.collect()
    catalog = build_dbt(directory, export_dir, run.id)
    with duckdb.connect(str(catalog), read_only=True) as connection:
        report = compare_report(connection, run)
    status = "PASS" if all(row["matches"] for row in report["comparisons"]) else "DRIFT"
    catalog_key = put_private(catalog.read_bytes(), max_bytes=MAX_CATALOG_BYTES)
    report["loaded_artifacts"] = loaded
    result_sha = canonical_hash(report)
    artifacts = list(
        db.scalars(
            select(AnalyticsArtifact)
            .where(AnalyticsArtifact.run_id == run.id)
            .order_by(AnalyticsArtifact.name)
        )
    )
    manifest = signed_manifest(
        {
            "schema_version": 1,
            "run_id": run.id,
            "source": {
                "snapshot_id": run.snapshot_id,
                "source_batch_id": inputs["source_batch_id"],
                "snapshot_sha256": inputs["snapshot_sha256"],
                "data_as_of": run.as_of.isoformat(),
            },
            "kind": run.kind,
            "parent_run_id": run.parent_run_id,
            "dropped_event_id": run.dropped_event_id,
            "model_version": run.model_version,
            "model_sha256": run.model_sha256,
            "inputs_sha256": run.inputs_sha256,
            "result_sha256": result_sha,
            "catalog_sha256": catalog_key,
            "status": status,
            "artifacts": [
                {
                    "name": item.name,
                    "sha256": item.storage_key,
                    "rows_sha256": item.rows_sha256,
                    "row_count": item.row_count,
                    "schema_version": item.schema_version,
                }
                for item in artifacts
            ],
            "loaded_artifacts": loaded,
            "delivery_event_ids": [
                row["event_id"]
                for row in inputs["cycles"]
                if row["event_id"] != run.dropped_event_id
            ],
            "comparisons": report["comparisons"],
            "cloud_validation": "UNVERIFIED",
        }
    )
    manifest_key = put_private(canonical_bytes(manifest))
    result = AnalyticsResult(
        run_id=run.id,
        status=status,
        catalog_key=catalog_key,
        manifest_key=manifest_key,
        report=report,
        sha256=result_sha,
    )
    db.add(result)
    db.flush()
    if status == "PASS":
        db.add(AnalyticsPublication(run_id=run.id, snapshot_id=run.snapshot_id))
    record(
        db,
        None,
        "analytics.result_created",
        "analytics_run",
        run.id,
        run.request_id,
        after_hash=result_sha,
        emit=False,
    )
    db.flush()
    return result


def verified_result(db: Session, run: AnalyticsRun) -> tuple[AnalyticsResult, dict[str, Any]]:
    result = db.get(AnalyticsResult, run.id)
    if result is None:
        raise DomainError(
            409,
            "analytics_pending",
            "The independent analytical worker has not completed this run.",
        )
    try:
        document: dict[str, Any] = json.loads(read_private(result.manifest_key))
        read_private(result.catalog_key, max_bytes=MAX_CATALOG_BYTES)
        if (
            not signature_valid(document)
            or result.sha256 != canonical_hash(result.report)
            or document["manifest"]["run_id"] != run.id
            or document["manifest"]["inputs_sha256"] != run.inputs_sha256
            or document["manifest"]["model_sha256"] != run.model_sha256
            or document["manifest"]["result_sha256"] != result.sha256
            or document["manifest"]["catalog_sha256"] != result.catalog_key
            or document["manifest"]["status"] != result.status
        ):
            raise ValueError("Analytical fingerprint mismatch")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DomainError(
            503,
            "analytics_artifact_unavailable",
            "The pinned analytical result is unavailable or corrupt. Restore its private artifact or create an explicit repair; no current data was substituted.",
        ) from error
    return result, document


def read_mart(db: Session, run: AnalyticsRun, name: str) -> list[dict[str, Any]]:
    result, _document = verified_result(db, run)
    with (
        materialize_private(result.catalog_key, max_bytes=MAX_CATALOG_BYTES) as catalog,
        duckdb.connect(str(catalog), read_only=True) as connection,
    ):
        return sql_rows(connection, name)
