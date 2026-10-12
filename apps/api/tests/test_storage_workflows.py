"""Persisted domain workflows with the Supabase REST contract transport.

PostgreSQL, domain engines, DuckDB/dbt and consumers execute normally. Only the
external Storage HTTP service is simulated; these do not establish cloud access.
"""

from datetime import timedelta

import pytest
from meridian.models import RoleAssignment, now
from sqlalchemy import select
from test_analytics_api import analytics_worker as analytics_worker
from test_analytics_api import (
    test_real_locked_compensation_and_leap_day_forecast_drift_and_repair_are_exact as analytics_flow,
)
from test_analytics_crash import (
    test_crash_after_catalog_file_rolls_back_sql_receipts_then_safely_replays as analytics_crash,
)
from test_analytics_pipeline import analytics_storage as analytics_storage
from test_compensation_api import comp_worker as comp_worker
from test_compensation_api import drain, locked
from test_compensation_api import (
    test_http_return_resubmit_three_signatures_lock_export_reconcile as compensation_flow,
)
from test_compensation_api import (
    test_worker_duplicate_and_export_after_file_crash as compensation_crash,
)
from test_ingestion import detail, publish, register, upload
from test_ingestion import ingestion_worker as ingestion_worker
from test_object_store import hosted_storage as hosted_storage
from test_replay import (
    test_locked_returned_award_replays_exact_amounts_and_human_hierarchy as replay_flow,
)
from test_talent_api import talent_worker as talent_worker
from test_talent_api import (
    test_http_return_calibrate_lock_reopen_history_and_exact_export as talent_flow,
)

from scripts.generate_workforce_fixtures import csv_bytes, fixture_rows


def test_remote_ingestion_quarantine_correct_publish(
    client, login, db, ingestion_worker, hosted_storage
):
    login("steward")
    invalid = register(client, upload(client, csv_bytes(fixture_rows(True))))
    assert ingestion_worker() == 1
    invalid = detail(client, invalid)
    assert (invalid["accepted_count"], invalid["rejected_count"]) == (4960, 40)
    assert publish(client, invalid).status_code == 409
    corrected = register(
        client, upload(client, csv_bytes(fixture_rows())), supersedes_batch_id=invalid["id"]
    )
    assert ingestion_worker() == 1
    corrected = detail(client, corrected)
    assert (corrected["accepted_count"], corrected["rejected_count"]) == (5000, 0)
    published = publish(client, corrected)
    assert published.status_code == 200
    assert ingestion_worker() == 1
    assert detail(client, published.json())["manifest_ready"]
    assert len(hosted_storage.objects) == 3


def test_remote_compensation_reconciliation(client, login, db, comp_worker, hosted_storage):
    compensation_flow(client, login, db, comp_worker)
    assert hosted_storage.objects


def test_remote_export_post_write_pre_ack_crash(
    client, login, db, comp_worker, hosted_storage, monkeypatch
):
    compensation_crash(client, login, db, comp_worker, monkeypatch)
    assert len(hosted_storage.objects) == 2


def test_remote_talent_lock_reopen_retains_old_bytes(
    client, login, db, talent_worker, hosted_storage
):
    talent_flow(client, login, db, talent_worker)
    assert len(hosted_storage.objects) == 2


def test_remote_analytics_reconciles_drift_and_repairs(
    client, login, db, comp_worker, analytics_worker, hosted_storage
):
    analytics_flow(client, login, db, comp_worker, analytics_worker)
    assert hosted_storage.objects


def test_remote_analytics_catalog_pre_ack_crash(
    client, login, db, analytics_worker, hosted_storage, monkeypatch
):
    analytics_crash(client, login, db, analytics_worker, monkeypatch)
    assert hosted_storage.objects


def test_remote_replay_keeps_financial_and_redacted_packets_exact(
    client, login, db, comp_worker, hosted_storage
):
    replay_flow(client, login, db, comp_worker)
    assert hosted_storage.objects


@pytest.mark.security
def test_download_rechecks_grants_before_reading_remote_storage(
    client, login, db, comp_worker, hosted_storage
):
    cycle, _ = locked(client, login, db, comp_worker)
    drain(comp_worker, db)
    login("finance")
    exported = client.get(f"/api/v1/compensation/cycles/{cycle['id']}/export")
    assert exported.status_code == 200
    token = exported.json()["download_token"]
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "finance"))
    grant.effective_to = now() - timedelta(seconds=1)
    db.commit()
    reads = hosted_storage.reads
    denied = client.get(
        f"/api/v1/compensation/cycles/{cycle['id']}/download", params={"token": token}
    )
    assert denied.status_code == 403
    assert hosted_storage.reads == reads
    login("executive")
    assert client.get(f"/api/v1/compensation/cycles/{cycle['id']}/export").status_code == 403
    assert hosted_storage.reads == reads
