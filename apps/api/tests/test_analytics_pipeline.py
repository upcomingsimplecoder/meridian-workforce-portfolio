"""Actual PostgreSQL → deterministic Parquet → dbt/DuckDB numerical and integrity gates."""

import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import duckdb
import pytest
from meridian import analytics_pipeline
from meridian.analytics_exports import extract, parquet_bytes
from meridian.analytics_models import AnalyticsArtifact, AnalyticsPublication
from meridian.analytics_pipeline import execute_run, read_mart, signature_valid, verified_result
from meridian.analytics_schemas import AnalyticsRequest
from meridian.analytics_service import create_run
from meridian.config import settings
from meridian.errors import DomainError
from meridian.models import OutboxEvent
from meridian.object_store import ensure_key, object_path, read_private
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def analytics_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings(), "local_data_dir", str(tmp_path))
    ensure_key()
    return tmp_path


def source_run(db, source=CURRENT_SNAPSHOT):
    return create_run(
        db,
        AnalyticsRequest(
            snapshot_id=source,
            as_of=date(2027, 6, 30),
            reason="Verify exact synthetic source marts",
        ),
        actor_id="hr",
        request_id=str(uuid4()),
    )


def test_real_source_to_dbt_marts_reconciles_5000_people_5002_intervals_and_monthly_transfers(
    db, analytics_storage
):
    run = source_run(db)
    result = execute_run(db, run)
    assert result.status == "PASS", [
        row for row in result.report["comparisons"] if not row["matches"]
    ]
    assert result.report["headcount"] == 5000
    assert result.report["fte"] == "4900"
    assert result.report["employee_versions"] == 5002
    assert len(result.report["comparisons"]) == 23
    assert all(row["matches"] for row in result.report["comparisons"])
    assert (
        db.scalar(
            select(func.count())
            .select_from(AnalyticsPublication)
            .where(AnalyticsPublication.run_id == run.id)
        )
        == 1
    )
    _, manifest = verified_result(db, run)
    assert signature_valid(manifest)
    assert manifest["manifest"]["cloud_validation"] == "UNVERIFIED"
    assert len(manifest["manifest"]["artifacts"]) == 10
    assert b"base_pay" not in read_private(manifest["manifest"]["artifacts"][0]["sha256"])
    monthly = read_mart(db, run, "fct_headcount_month")
    d01 = {row["month_start"]: row for row in monthly if row["org_id"] == "D01"}
    assert d01["2027-02-01"]["closing_headcount"] == 417
    assert d01["2027-03-01"]["closing_headcount"] == 415
    assert d01["2027-03-01"]["transfers_out"] == 2
    assert all(
        row["opening_headcount"]
        + row["hires"]
        + row["transfers_in"]
        - row["departures"]
        - row["transfers_out"]
        == row["closing_headcount"]
        for row in monthly
    )
    assert execute_run(db, run).sha256 == result.sha256
    assert (
        db.scalar(
            select(func.count())
            .select_from(AnalyticsArtifact)
            .where(AnalyticsArtifact.run_id == run.id)
        )
        == 10
    )
    outcomes = json.loads(
        (analytics_storage / "analytics/runs" / run.id / "dbt-target/run_results.json").read_text()
    )
    assert all(item["status"] in {"success", "pass"} for item in outcomes["results"])


def test_corrected_source_is_a_new_mart_and_old_history_and_bytes_remain_unchanged(
    db, analytics_storage
):
    old = source_run(db, BASE_SNAPSHOT)
    old_result = execute_run(db, old)
    before = read_private(old_result.manifest_key)
    current = source_run(db)
    corrected = execute_run(db, current)
    assert old_result.report["employee_versions"] == 5000
    assert corrected.report["employee_versions"] == 5002
    old_march = next(
        row
        for row in read_mart(db, old, "fct_headcount_month")
        if row["org_id"] == "D01" and row["month_start"] == "2027-03-01"
    )
    new_march = next(
        row
        for row in read_mart(db, current, "fct_headcount_month")
        if row["org_id"] == "D01" and row["month_start"] == "2027-03-01"
    )
    assert old_march["closing_headcount"] == 417
    assert new_march["closing_headcount"] == 415
    assert read_private(old_result.manifest_key) == before
    assert old.inputs_sha256 != current.inputs_sha256


def test_parquet_is_byte_deterministic_typed_and_contains_no_names_or_salaries(db, tmp_path):
    data, _pins = extract(db, CURRENT_SNAPSHOT, date(2027, 6, 30), [], [])
    for name, rows in data.items():
        assert parquet_bytes(name, rows) == parquet_bytes(name, rows)
    path = tmp_path / "workforce.parquet"
    path.write_bytes(parquet_bytes("workforce", data["workforce"]))
    with duckdb.connect() as connection:
        columns = connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
        ).fetchall()
        assert "display_name" not in {row[0] for row in columns}
        assert "annual_pay_minor" not in {row[0] for row in columns}
        assert next(row[1] for row in columns if row[0] == "fte") == "DECIMAL(5,4)"
        assert (
            connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
            == 5002
        )


def test_request_and_delivery_intent_roll_back_together(db):
    with db.begin_nested() as nested:
        run = source_run(db)
        run_id = run.id
        assert db.scalar(select(OutboxEvent.id).where(OutboxEvent.aggregate_id == run_id))
        nested.rollback()
    assert db.scalar(select(OutboxEvent.id).where(OutboxEvent.aggregate_id == run_id)) is None


def test_model_change_fails_closed_before_creating_any_artifact(db, analytics_storage, monkeypatch):
    run = source_run(db)
    monkeypatch.setattr("meridian.analytics_pipeline.model_fingerprint", lambda: "a" * 64)
    with pytest.raises(RuntimeError, match="PINNED_MODEL"):
        execute_run(db, run)
    assert db.scalar(select(func.count()).select_from(AnalyticsArtifact)) == 0


def test_dbt_uses_the_configured_bounded_worker_timeout(monkeypatch, tmp_path):
    observed = {}

    def run(command, *, env, capture_output, timeout):
        observed.update(env=env, capture_output=capture_output, timeout=timeout)
        target = tmp_path / "dbt-target"
        target.mkdir()
        (target / "run_results.json").write_text(
            json.dumps({"results": [{"status": "success"}, {"status": "pass"}]})
        )
        Path(env["MERIDIAN_ANALYTICS_PATH"]).write_bytes(b"DuckDB catalog")
        return subprocess.CompletedProcess(command, 0, stdout=b"verified", stderr=b"")

    monkeypatch.setattr(
        analytics_pipeline,
        "settings",
        lambda: SimpleNamespace(analytics_dbt_timeout_seconds=600),
    )
    monkeypatch.setattr(analytics_pipeline.subprocess, "run", run)

    catalog = analytics_pipeline.build_dbt(tmp_path, tmp_path / "loaded", "synthetic-run")

    assert observed["timeout"] == 600
    assert observed["capture_output"] is True
    assert observed["env"]["MERIDIAN_DBT_THREADS"] == "1"
    assert observed["env"]["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false"
    assert catalog.read_bytes() == b"DuckDB catalog"
    assert (tmp_path / "dbt-build.log").read_bytes() == b"verified"


@pytest.mark.security
def test_sql_cannot_rewrite_run_or_publish_without_verified_result(db):
    run = source_run(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text("UPDATE analytics_run SET reason='Rewrite observed pins' WHERE id=:id"),
            {"id": run.id},
        )
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(AnalyticsPublication(run_id=run.id, snapshot_id=run.snapshot_id))
        db.flush()


def test_corrupt_catalog_fails_closed_without_substituting_live_data(db, analytics_storage):
    run = source_run(db)
    result = execute_run(db, run)
    object_path(result.catalog_key).write_bytes(b"corrupted test artifact")
    with pytest.raises(DomainError) as error:
        verified_result(db, run)
    assert error.value.status == 503
    assert "substituted" in error.value.detail
