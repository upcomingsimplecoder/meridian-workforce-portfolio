"""Persisted analytical journeys with real dbt and current object/field authority."""

import json
import subprocess
from datetime import timedelta

import pytest
from meridian.analytics_models import (
    AnalyticsArtifact,
    AnalyticsDelivery,
    AnalyticsPublication,
    AnalyticsResult,
    AnalyticsRun,
)
from meridian.analytics_pipeline import verified_result
from meridian.analytics_service import event_for
from meridian.models import (
    AuditEvent,
    ConsumerReceipt,
    RoleAssignment,
    WorkerHeartbeat,
    now,
    uid,
)
from meridian.people_models import Snapshot
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from meridian.scenario_models import ScenarioRevision
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_analytics_pipeline import analytics_storage as analytics_storage
from test_analytics_pipeline import source_run
from test_compensation_api import comp_worker as comp_worker
from test_compensation_api import drain, locked
from test_scenarios import add, hire, plan

from apps.worker import analytics as worker

ROOT = "/api/v1/analytics"


@pytest.fixture
def analytics_worker(db, analytics_storage, monkeypatch):
    monkeypatch.setattr(
        worker,
        "SessionLocal",
        sessionmaker(
            bind=db.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
    )

    def tick():
        result = worker.tick()
        db.expire_all()
        return result

    return tick


def start(client, **changes):
    return client.post(
        ROOT + "/runs",
        json={
            "snapshot_id": CURRENT_SNAPSHOT,
            "as_of": "2027-06-30",
            "reason": "Reconcile actual synthetic source versions",
        }
        | changes,
        headers={"Idempotency-Key": uid()},
    )


def derived(client, run, route, body):
    return client.post(
        f"{ROOT}/runs/{run['id']}/{route}", json=body, headers={"Idempotency-Key": uid()}
    )


def get(client, run):
    response = client.get(f"{ROOT}/runs/{run['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_real_locked_compensation_and_leap_day_forecast_drift_and_repair_are_exact(
    client, login, db, comp_worker, analytics_worker
):
    cycle, proposals = locked(client, login, db, comp_worker)
    drain(comp_worker, db)
    scenario = plan(client, scope_orgs=[f"D{i:02d}" for i in range(1, 13)])
    response = add(client, scenario, hire())
    assert response.status_code == 200
    scenario = response.json()
    drain(comp_worker, db)
    before_cycle = client.get(f"/api/v1/compensation/cycles/{cycle['id']}").json()
    response = start(
        client,
        cycle_ids=[cycle["id"]],
        scenario_revision_ids=[
            db.scalar(
                select(ScenarioRevision.id).where(
                    ScenarioRevision.scenario_id == scenario["id"], ScenarioRevision.number == 2
                )
            )
        ],
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "QUEUED" and run["headcount"] is None
    assert analytics_worker() == 1
    run = get(client, run)
    assert run["status"] == "PASS", [row for row in run["comparisons"] if not row["matches"]]
    assert run["compensation"][0] == {
        "cycle_id": cycle["id"],
        "currency": "USD",
        "exponent": 2,
        "row_count": 3,
        "annualized_merit_minor": "1220000",
        "merit_first_year_minor": "813328",
        "bonus_payable_minor": "300000",
        "pool_charge_minor": "1520000",
        "budget_minor": "2500000",
        "holdback_minor": "100000",
        "remaining_minor": "880000",
        "reporting_currency": "USD",
        "reporting_pool_charge_minor": "1520000",
    }
    assert run["scenarios"][0]["annual_minor"] == "77728752452"
    assert run["scenarios"][0]["baseline_minor"] == "77714704176"
    assert run["scenarios"][0]["delta_minor"] == "14048276"
    assert all(row["matches"] for row in run["comparisons"])
    original_manifest = client.get(f"{ROOT}/runs/{run['id']}/manifest").content
    event_id = run["delivery_event_ids"][0]
    response = derived(
        client,
        run,
        "gap-drill",
        {
            "event_id": event_id,
            "reason": "Simulate a missed delivery without modifying the locked source",
        },
    )
    assert response.status_code == 201, response.text
    gap = response.json()
    assert analytics_worker() == 1
    gap = get(client, gap)
    assert gap["status"] == "DRIFT" and gap["pipeline_status"] == "DRIFT"
    assert gap["headcount"] == 5000 and gap["scenarios"] == run["scenarios"]
    assert gap["compensation"][0]["row_count"] == 0
    assert gap["compensation"][0]["reporting_currency"] == "USD"
    assert gap["compensation"][0]["pool_charge_minor"] == "0"
    assert any(
        row["expected"] == "1520000" and row["observed"] == "0" and not row["matches"]
        for row in gap["comparisons"]
    )
    assert not db.scalar(
        select(AnalyticsPublication.id).where(AnalyticsPublication.run_id == gap["id"])
    )
    assert not db.get(AnalyticsDelivery, (gap["id"], event_id))
    assert client.get(f"/api/v1/compensation/cycles/{cycle['id']}").json() == before_cycle
    assert client.get(f"{ROOT}/runs/{run['id']}/manifest").content == original_manifest
    response = derived(
        client, gap, "repair", {"reason": "Replay the exact frozen versions after the missed event"}
    )
    assert response.status_code == 201, response.text
    repaired = response.json()
    assert analytics_worker() == 1
    repaired = get(client, repaired)
    assert repaired["status"] == "PASS" and repaired["kind"] == "REPAIR"
    assert repaired["inputs_sha256"] == run["inputs_sha256"] == gap["inputs_sha256"]
    assert repaired["result_sha256"] == run["result_sha256"]
    assert repaired["artifacts"] == run["artifacts"] == gap["artifacts"]
    assert db.get(AnalyticsDelivery, (repaired["id"], event_id)).row_count == 3
    assert db.scalar(select(func.count()).select_from(AnalyticsPublication)) == 2
    assert {employee_id: proposal["revision"] for employee_id, proposal in proposals.items()} == {
        "SYN-000025": 2,
        "SYN-000049": 4,
        "SYN-000073": 2,
    }
    assert client.get(f"{ROOT}/runs/{gap['id']}").json()["status"] == "DRIFT"


@pytest.mark.security
@pytest.mark.parametrize(
    "persona", ["manager-a", "manager-b", "executive", "steward", "approver", "auditor"]
)
def test_restricted_personas_cannot_retrieve_full_run_options_manifest_or_create(
    client, login, db, persona
):
    run = source_run(db)
    login(persona)
    for path in ("/options", "/runs", f"/runs/{run.id}", f"/runs/{run.id}/manifest"):
        response = client.get(ROOT + path)
        assert response.status_code == 403
        assert "annualized_merit_minor" not in response.text and "SYN-" not in response.text
    assert start(client).status_code == 403
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.actor_id == persona, AuditEvent.permission_result == "DENY")
        )
        >= 5
    )


def test_current_authority_precedes_idempotency_replay_and_csrf(client, login, db):
    login()
    key = uid()
    payload = {
        "snapshot_id": CURRENT_SNAPSHOT,
        "as_of": "2027-06-30",
        "reason": "Pin an actual synthetic source",
    }
    first = client.post(ROOT + "/runs", json=payload, headers={"Idempotency-Key": key})
    second = client.post(ROOT + "/runs", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert (
        client.post(
            ROOT + "/runs",
            json=payload,
            headers={"Idempotency-Key": uid(), "X-CSRF-Token": "invalid"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            ROOT + "/runs",
            json=payload,
            headers={"Idempotency-Key": uid(), "Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            ROOT + "/runs", json=payload | {"as_of": "2027-05-30"}, headers={"Idempotency-Key": key}
        ).status_code
        == 409
    )
    role = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "hr"))
    role.role = "executive"
    db.commit()
    assert (
        client.post(ROOT + "/runs", json=payload, headers={"Idempotency-Key": key}).status_code
        == 403
    )
    assert client.get(f"{ROOT}/runs/{first.json()['id']}/manifest").status_code == 403


@pytest.mark.security
def test_finance_cannot_bypass_private_scenario_scope_using_analytics_or_manifest(
    client, login, db, comp_worker, analytics_worker
):
    login()
    # A real second planner, with narrowly granted planning authority, reviews this plan.
    # Finance remains unrelated. This test never broadens Finance's object scope.
    reviewer = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "approver"))
    reviewer.role = "hr"
    db.commit()
    private = plan(client, reviewer_id="approver", scope_orgs=["D01"])
    drain(comp_worker, db)
    revision_id = db.scalar(
        select(ScenarioRevision.id).where(
            ScenarioRevision.scenario_id == private["id"], ScenarioRevision.number == 1
        )
    )
    response = start(client, scenario_revision_ids=[revision_id])
    assert response.status_code == 201
    run = response.json()
    assert analytics_worker() == 1
    login("finance")
    assert revision_id not in json.dumps(client.get(ROOT + "/options").json())
    assert start(client, scenario_revision_ids=[revision_id]).status_code == 404
    assert client.get(f"{ROOT}/runs/{run['id']}").status_code == 404
    assert client.get(f"{ROOT}/runs/{run['id']}/manifest").status_code == 404
    assert client.get(ROOT + "/runs").json()["items"] == []
    assert client.get(ROOT + "/runs", params={"cursor": run["id"]}).status_code == 422


def test_executive_and_auditor_receive_separate_allowlisted_projections_and_stale_labels(
    client, login, db, analytics_worker
):
    login()
    assert start(client).status_code == 201
    assert analytics_worker() == 1
    login("executive")
    response = client.get(ROOT + "/public")
    assert response.status_code == 200
    public = response.json()
    assert public["headcount"] == 5000 and public["fte"] == "4900"
    assert public["pipeline_status"] == "CURRENT"
    assert set(public) == {
        "pipeline_status",
        "data_as_of",
        "source_watermark",
        "last_successful_sync",
        "headcount",
        "fte",
        "model_version",
        "cloud_validation",
        "suppressed",
    }
    assert client.get(ROOT + "/evidence").status_code == 403
    login("auditor")
    evidence = client.get(ROOT + "/evidence").json()[0]
    assert evidence["comparison_count"] == 23 and evidence["failed_comparisons"] == 0
    assert evidence["source_versions"] == 5002
    assert (
        "headcount" not in evidence
        and "compensation" not in evidence
        and "SYN-" not in json.dumps(evidence)
    )
    login()
    assert start(client).status_code == 201
    login("executive")
    assert client.get(ROOT + "/public").json()["pipeline_status"] == "STALE"
    heartbeat = db.get(WorkerHeartbeat, "analytics")
    heartbeat.last_seen_at = now() - timedelta(minutes=2)
    db.commit()
    assert client.get(ROOT + "/public").json()["pipeline_status"] == "OUTAGE"


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("missing_heartbeat", [False, True])
def test_completed_run_outage_and_recovery_preserve_results_and_match_public_freshness(
    client, login, db, analytics_worker, monkeypatch, historical, missing_heartbeat
):
    from meridian import analytics, analytics_projection

    login()
    run = start(client).json()
    assert analytics_worker() == 1
    if historical:
        assert start(client).status_code == 201
    before = get(client, run)
    assert before["status"] == "PASS"
    assert before["pipeline_status"] == ("HISTORICAL" if historical else "CURRENT")
    assert before["historical"] is historical
    manifest = client.get(f"{ROOT}/runs/{run['id']}/manifest").content
    measured = now()
    monkeypatch.setattr(analytics_projection, "now", lambda: measured)
    monkeypatch.setattr(analytics, "now", lambda: measured)
    heartbeat = db.get(WorkerHeartbeat, "analytics")
    assert heartbeat is not None
    heartbeat.last_seen_at = measured - timedelta(seconds=60)
    db.commit()
    assert get(client, run)["pipeline_status"] == before["pipeline_status"]
    if missing_heartbeat:
        db.delete(heartbeat)
    else:
        heartbeat.last_seen_at = measured - timedelta(seconds=61)
    db.commit()
    unavailable = get(client, run)
    assert unavailable["pipeline_status"] == "OUTAGE"
    assert unavailable["status"] == "PASS"
    assert unavailable["historical"] is historical
    for field in (
        "result_sha256",
        "inputs_sha256",
        "last_successful_sync",
        "headcount",
        "fte",
        "comparisons",
        "artifacts",
    ):
        assert unavailable[field] == before[field]
    assert (
        next(row for row in client.get(ROOT + "/runs").json()["items"] if row["id"] == run["id"])[
            "pipeline_status"
        ]
        == "OUTAGE"
    )
    assert client.get(f"{ROOT}/runs/{run['id']}/manifest").content == manifest
    login("executive")
    assert client.get(ROOT + "/public").json()["pipeline_status"] == "OUTAGE"
    if missing_heartbeat:
        db.add(WorkerHeartbeat(name="analytics", last_seen_at=measured))
    else:
        heartbeat.last_seen_at = measured
    db.commit()
    assert client.get(ROOT + "/public").json()["pipeline_status"] == (
        "STALE" if historical else "CURRENT"
    )
    login()
    restored = get(client, run)
    assert restored["pipeline_status"] == before["pipeline_status"]
    assert restored["result_sha256"] == before["result_sha256"]
    assert restored["last_successful_sync"] == before["last_successful_sync"]
    assert client.get(f"{ROOT}/runs/{run['id']}/manifest").content == manifest


def test_duplicate_delivery_has_one_receipt_and_does_not_rebuild_catalog(
    client, login, db, analytics_worker, monkeypatch
):
    login()
    run = start(client).json()
    assert analytics_worker() == 1
    model = db.get(AnalyticsRun, run["id"])
    event = event_for(db, model)
    before = verified_result(db, model)[0].sha256

    def unexpected_rebuild(*args):
        raise AssertionError("A completed delivery must not rebuild dbt")

    monkeypatch.setattr(worker, "execute_run", unexpected_rebuild)
    event.state = "PENDING"
    db.commit()
    assert analytics_worker() == 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(ConsumerReceipt)
            .where(ConsumerReceipt.consumer == "analytics-v1", ConsumerReceipt.event_id == event.id)
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(AnalyticsArtifact)
            .where(AnalyticsArtifact.run_id == run["id"])
        )
        == 10
    )
    assert verified_result(db, model)[0].sha256 == before


def test_analytics_outage_and_poison_job_retries_are_honest_and_do_not_block_domain_work(
    client, login, db, comp_worker, analytics_worker, monkeypatch
):
    login()
    run = start(client).json()
    model = db.get(AnalyticsRun, run["id"])
    event = event_for(db, model)

    def unavailable(*args):
        raise ConnectionError("Synthetic analytical adapter outage")

    monkeypatch.setattr(worker, "execute_run", unavailable)
    for attempt in range(1, 6):
        event.next_attempt_at = now() - timedelta(seconds=1)
        db.commit()
        assert analytics_worker() == 0
        assert event.attempts == attempt
        assert event.state == ("DEAD" if attempt == 5 else "PENDING")
        assert event.last_error == "ANALYTICS_EXECUTION_FAILED"
    assert db.get(AnalyticsResult, run["id"]) is None
    assert db.get(ConsumerReceipt, ("analytics-v1", event.id)) is None
    assert get(client, run)["status"] == "FAILED"
    scenario = plan(client)
    drain(comp_worker, db)
    assert client.get(f"/api/v1/scenarios/{scenario['id']}").json()["verified"]
    retry = f"{ROOT}/runs/{run['id']}/retry"
    assert (
        client.post(
            retry,
            json={"reason": "Recover the original pinned job"},
            headers={"If-Match": "4", "Idempotency-Key": uid()},
        ).status_code
        == 409
    )
    assert (
        client.post(
            retry,
            json={"reason": "Recover the original pinned job"},
            headers={"If-Match": "5", "Idempotency-Key": uid()},
        ).status_code
        == 200
    )
    assert event.state == "PENDING" and event.attempts == 0
    event.schema_version = 99
    db.commit()
    assert analytics_worker() == 0
    assert event.last_error == "UNSUPPORTED_EVENT_SCHEMA"


def test_analytics_worker_records_a_sanitized_timeout_class_and_retries(
    client, login, db, analytics_worker, monkeypatch
):
    login()
    run = start(client).json()
    model = db.get(AnalyticsRun, run["id"])
    event = event_for(db, model)

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(["dbt", "build"], timeout=600)

    monkeypatch.setattr(worker, "execute_run", timed_out)
    assert analytics_worker() == 0
    assert event.attempts == 1
    assert event.state == "PENDING"
    assert event.last_error == "ANALYTICS_EXECUTION_TIMEOUT"
    assert db.get(AnalyticsResult, run["id"]) is None
    assert db.get(ConsumerReceipt, ("analytics-v1", event.id)) is None


@pytest.mark.security
def test_sql_cannot_publish_empty_or_falsely_matching_comparison_reports(db):
    for comparisons in ([], [{"matches": True, "expected": "12", "observed": "11"}] * 23):
        run = source_run(db)
        db.add(
            AnalyticsResult(
                run_id=run.id,
                status="PASS",
                catalog_key="a" * 64,
                manifest_key="b" * 64,
                sha256="c" * 64,
                report={
                    "inputs_sha256": run.inputs_sha256,
                    "model_sha256": run.model_sha256,
                    "model_version": run.model_version,
                    "comparisons": comparisons,
                },
            )
        )
        db.flush()
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(AnalyticsPublication(run_id=run.id, snapshot_id=run.snapshot_id))
            db.flush()


@pytest.mark.parametrize(
    "change",
    ["schema", "source", "correlation", "aggregate_version", "input_hash", "unknown_field"],
)
def test_worker_rejects_poison_or_misbound_envelopes_before_running_models(
    client, login, db, analytics_worker, monkeypatch, change
):
    login()
    response = start(client)
    assert response.status_code == 201
    model = db.get(AnalyticsRun, response.json()["id"])
    event = event_for(db, model)
    if change == "schema":
        event.schema_version = 2
    elif change == "source":
        event.payload = event.payload | {
            "source_batch_id": db.get(Snapshot, BASE_SNAPSHOT).source_batch_id
        }
    elif change == "correlation":
        event.request_id = uid()
    elif change == "aggregate_version":
        event.aggregate_version = 2
    elif change == "input_hash":
        event.payload = event.payload | {"inputs_sha256": "a" * 64}
    else:
        event.payload = event.payload | {"restricted_award": "unexpected"}

    def forbidden(*args):
        raise AssertionError("Misbound event must be rejected before any model runs")

    monkeypatch.setattr(worker, "execute_run", forbidden)
    db.commit()
    assert analytics_worker() == 0
    assert event.attempts == 1
    assert db.get(ConsumerReceipt, ("analytics-v1", event.id)) is None
    assert db.get(AnalyticsResult, model.id) is None


@pytest.mark.security
def test_full_capability_on_department_scope_cannot_amplify_to_global_analytics(client, login, db):
    login()
    role = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "hr"))
    role.scope = "D01"
    db.commit()
    assert start(client).status_code == 403
    assert client.get(ROOT + "/runs").status_code == 403
    assert client.get(ROOT + "/options").status_code == 403
