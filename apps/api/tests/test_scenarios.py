"""Real PostgreSQL scenario journeys, exact numbers, permissions and worker recovery."""

import json
from datetime import timedelta

import pytest
from meridian import object_store
from meridian.finance_seed import FX_BASE_ID, FX_SHOCK_ID, POLICY_ID
from meridian.ingestion_models import IdempotencyReceipt
from meridian.models import (
    AuditEvent,
    ConsumerReceipt,
    OutboxEvent,
    Principal,
    RoleAssignment,
    now,
    uid,
)
from meridian.people_models import EmployeeVersion, Snapshot
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from meridian.scenario_models import (
    PlanningBaseline,
    Scenario,
    ScenarioDecision,
    ScenarioManifest,
    ScenarioRevision,
    ScenarioVerification,
)
from meridian.scenario_service import verify_revision, write_approved_manifest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from apps.worker import run as worker

ROOT = "/api/v1/scenarios"


@pytest.fixture
def planning_worker(db, monkeypatch, tmp_path):
    from meridian.config import settings

    monkeypatch.setattr(settings(), "local_data_dir", str(tmp_path))
    object_store.ensure_key()
    monkeypatch.setattr(
        worker,
        "SessionLocal",
        sessionmaker(
            bind=db.bind,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        ),
    )

    def tick():
        result = worker.tick()
        db.expire_all()
        return result

    return tick


def create(client, **changes):
    body = (
        dict(
            title="Synthetic annual plan",
            base_snapshot_id=CURRENT_SNAPSHOT,
            policy_id=POLICY_ID,
            fx_set_id=FX_BASE_ID,
            reviewer_id="finance",
            year=2028,
            planning_date="2027-12-01",
            scope_orgs=["D01", "D02"],
            budget_minor="99999999999999999999",
        )
        | changes
    )
    return client.post(ROOT, json=body, headers={"Idempotency-Key": uid()})


def plan(client, **changes):
    response = create(client, **changes)
    assert response.status_code == 201, response.text
    return response.json()


def command(client, scenario, path, body=None, **headers):
    return client.post(
        f"{ROOT}/{scenario['id']}/{path}",
        json=body or {"reason": "Synthetic human planning decision"},
        headers={"If-Match": str(scenario["version"]), "Idempotency-Key": uid()} | headers,
    )


def hire(**changes):
    return (
        dict(
            kind="hire",
            id=uid(),
            effective_date="2028-02-29",
            reason="Synthetic leap day hire",
            employee_id="SYN-900001",
            position_id="POS-900001",
            org_id="D01",
            manager_id=None,
            grade="L1",
            annual_pay_minor="12000000",
            currency="USD",
            fte="1.0000",
        )
        | changes
    )


def add(client, scenario, op):
    return command(
        client, scenario, "operations", {"reason": "Save a synthetic operation", "operations": [op]}
    )


def get(client, scenario):
    response = client.get(f"{ROOT}/{scenario['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def forecast(client, scenario, **query):
    response = client.get(f"{ROOT}/{scenario['id']}/forecast", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_persisted_ab_return_revision_approval_manifest_promotion(
    client, login, db, planning_worker
):
    login()
    base = plan(client, scope_orgs=[f"D{i:02d}" for i in range(1, 13)])
    assert base["annual_total_minor"] == "77714704176"
    a = command(
        client,
        base,
        "branches",
        {
            "title": "Hire option A",
            "reviewer_id": "finance",
            "reason": "Compare independent options",
        },
    ).json()
    b = command(
        client,
        base,
        "branches",
        {
            "title": "Unchanged option B",
            "reviewer_id": "finance",
            "reason": "Preserve the alternative",
        },
    ).json()
    saved = add(client, a, hire())
    assert saved.status_code == 200, saved.text
    a = saved.json()
    assert (a["revision"], a["version"], len(a["operations"])) == (2, 2, 1)
    assert get(client, b)["revision"] == 1 and get(client, base)["operations"] == []
    delta = forecast(client, a)
    # Independent calendar arithmetic: ten full months + one day / 29 in February.
    assert delta["annual_delta_minor"] == "14048276"
    assert delta["annual_component_delta"] == {
        "base_minor": "10034483",
        "benefits_minor": "2508621",
        "variable_minor": "1003448",
        "employer_tax_minor": "501724",
        "total_minor": "14048276",
    }
    assert delta["monthly_delta_minor"] == ["0", "48276"] + ["1400000"] * 10
    assert delta["current"]["result"]["annual"]["total_minor"] == "77728752452"
    assert forecast(client, a) == delta
    comparison = client.get(
        ROOT + "/compare", params=[("ids", base["id"]), ("ids", a["id"]), ("ids", b["id"])]
    ).json()
    assert [item["annual_delta_minor"] for item in comparison["items"]] == ["0", "14048276", "0"]
    assert command(client, a, "submit").status_code == 409
    for _ in range(4):
        assert planning_worker() == 1
    a = command(client, a, "submit").json()
    assert a["state"] == "SUBMITTED"
    assert (
        add(client, a, hire(employee_id="SYN-900002", position_id="POS-900002")).status_code == 409
    )
    assert command(client, a, "review").status_code == 403
    login("finance")
    a = command(client, a, "review").json()
    a = command(
        client, a, "decisions", {"decision": "return", "reason": "Explain the hiring rationale"}
    ).json()
    assert a["state"] == "REJECTED"
    login()
    a = command(client, a, "undo", {"reason": "Reconsider proposed hire"}).json()
    assert a["revision"] == 3 and a["state"] == "DRAFT" and a["operations"] == []
    assert forecast(client, a, revision=2)["annual_delta_minor"] == "14048276"
    assert forecast(client, a)["annual_delta_minor"] == "0"
    assert planning_worker() == 1
    a = command(client, a, "submit").json()
    login("finance")
    a = command(client, a, "review").json()
    a = command(
        client,
        a,
        "decisions",
        {"decision": "approve", "reason": "Approve the revised funding plan"},
    ).json()
    assert a["state"] == "APPROVED" and not a["manifest_ready"]
    assert command(client, a, "promote").status_code == 409
    assert (
        db.scalar(
            select(func.count())
            .select_from(ScenarioDecision)
            .where(ScenarioDecision.action == "PROMOTE")
        )
        == 0
    )
    assert planning_worker() == 1
    manifest = client.get(f"{ROOT}/{a['id']}/manifest").json()
    assert manifest["document"]["approval"]["reason"] == "Approve the revised funding plan"
    stored = db.get(ScenarioManifest, a["revisions"][-1]["id"])
    assert json.loads(object_store.read_private(stored.storage_key)) == manifest["document"]
    a = command(client, a, "promote").json()
    assert a["state"] == "PROMOTED"
    assert client.get(ROOT + "/baselines").json()[0]["scenario_id"] == a["id"]
    assert command(client, a, "undo").status_code == 403  # reviewer cannot edit owner log
    login()
    assert command(client, a, "undo").status_code == 409
    assert db.scalar(select(func.count()).select_from(EmployeeVersion)) == 10002
    assert db.get(Snapshot, CURRENT_SNAPSHOT).row_count == 5000
    exported = client.get(f"{ROOT}/{a['id']}/export", params={"revision": 2})
    assert exported.status_code == 200 and exported.headers["Cache-Control"] == "no-store"
    assert "annual_pay" not in exported.text and "SYN-900001" not in exported.text
    assert len(exported.text.splitlines()) == 145  # header + 12 months x 12 departments
    assert db.scalar(select(func.count()).select_from(PlanningBaseline)) == 1
    verbs = list(db.scalars(select(AuditEvent.verb)))
    assert "scenario.operation_added" in verbs and "scenario.operation_undone" in verbs


def test_idempotency_stale_revision_and_branch_pins(client, login, db):
    login()
    base = plan(client)
    key = uid()
    op = hire()
    first = command(
        client,
        base,
        "operations",
        {"reason": "Save hiring assumption", "operations": [op]},
        **{"Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text
    same = command(
        client,
        base,
        "operations",
        {"reason": "Save hiring assumption", "operations": [op]},
        **{"Idempotency-Key": key},
    )
    assert same.json() == first.json() and same.status_code == 200
    conflict = command(
        client,
        base,
        "operations",
        {"reason": "Changed input for same key", "operations": [op]},
        **{"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert (
        add(client, base, hire(employee_id="SYN-900002", position_id="POS-900002")).status_code
        == 409
    )
    saved = first.json()
    child = command(
        client,
        saved,
        "branches",
        {"title": "Pinned child", "reviewer_id": "finance", "reason": "Fork approved inputs"},
    ).json()
    assert child["parent_revision_id"] == saved["revisions"][-1]["id"]
    assert child["fx_set_id"] == FX_BASE_ID and child["operations"] == saved["operations"]
    undone = command(client, saved, "undo").json()
    assert get(client, child)["operations"] == saved["operations"]
    assert forecast(client, child)["annual_delta_minor"] == "0"
    assert forecast(client, undone, revision=2)["annual_delta_minor"] == "14048276"
    assert db.scalar(select(func.count()).select_from(ScenarioRevision)) == 4
    assert db.scalar(select(func.count()).select_from(IdempotencyReceipt)) == 4


@pytest.mark.parametrize(
    "change",
    [
        {"base_snapshot_id": uid()},
        {"policy_id": uid()},
        {"fx_set_id": uid()},
        {"budget_minor": "1"},
        {"year": 2032},
        {"scope_orgs": ["D01", "D01"]},
        {"scope_orgs": ["D99"]},
        {"planning_date": "2029-01-01"},
    ],
)
def test_failed_create_is_atomic(client, login, db, change):
    login()
    response = create(client, **change)
    assert response.status_code == 422, response.text
    db.commit()  # the next request must never commit a failed command's leftover state
    assert db.scalar(select(func.count()).select_from(Scenario)) == 0
    assert db.scalar(select(func.count()).select_from(ScenarioRevision)) == 0
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.security
@pytest.mark.parametrize(
    "role", ["executive", "manager-a", "manager-b", "approver", "auditor", "steward"]
)
def test_nonplanner_all_object_routes_fail_closed(client, login, role):
    login()
    scenario = plan(client)
    login(role)
    for route in [
        "",
        "/options",
        "/baselines",
        f"/{scenario['id']}",
        f"/{scenario['id']}/forecast",
        f"/{scenario['id']}/export",
        f"/{scenario['id']}/manifest",
    ]:
        response = client.get(ROOT + route)
        assert response.status_code == 403 and "annual_pay" not in response.text
    assert create(client).status_code == 403
    assert (
        command(
            client,
            scenario,
            "branches",
            {
                "title": "Hidden data",
                "reviewer_id": "finance",
                "reason": "Attempt unauthorized fork",
            },
        ).status_code
        == 403
    )
    for route in ["undo", "submit", "review", "promote", "retry"]:
        assert command(client, scenario, route).status_code == 403
    assert add(client, scenario, hire()).status_code == 403
    assert (
        command(
            client,
            scenario,
            "decisions",
            {"decision": "approve", "reason": "Attempt unauthorized approval"},
        ).status_code
        == 403
    )


@pytest.mark.security
def test_scope_revocation_object_export_and_mixed_grants(client, login, db):
    login()
    broad = plan(client)
    narrow = plan(client, scope_orgs=["D01"])
    login("finance")
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "finance"))
    grant.scope = "D01"
    db.commit()
    assert client.get(f"{ROOT}/{broad['id']}").status_code == 404
    assert client.get(f"{ROOT}/{broad['id']}/export").status_code == 404
    assert client.get(ROOT).json()["items"][0]["id"] == narrow["id"]
    assert client.get(f"{ROOT}/{narrow['id']}/export").status_code == 200
    # Broader steward read cannot amplify a narrower planner's export/planning grant.
    db.add(RoleAssignment(principal_id="finance", role="data_steward", scope="*"))
    db.commit()
    assert client.get(f"{ROOT}/{broad['id']}/forecast").status_code == 404
    grant.effective_to = now()
    db.commit()
    assert client.get(f"{ROOT}/{narrow['id']}/export").status_code == 403


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    [
        {"reviewer_id": "hr"},
        {"reviewer_id": "executive"},
        {"reviewer_id": "unknown"},
        {"owner_id": "finance"},
        {"state": "APPROVED"},
        {"role": "finance"},
    ],
)
def test_overposting_and_self_reviewer_rejected(client, login, db, change):
    login()
    response = create(client, **change)
    assert response.status_code in {403, 422}
    assert db.scalar(select(func.count()).select_from(Scenario)) == 0


@pytest.mark.security
def test_csrf_and_other_owner_and_revoked_reviewer(client, login, db):
    login()
    scenario = plan(client)
    response = add(client, scenario, hire())
    assert response.status_code == 200
    scenario = response.json()
    client.headers["X-CSRF-Token"] = "forged"
    assert command(client, scenario, "undo").status_code == 403
    login("finance")
    assert add(client, scenario, hire()).status_code == 403
    # Finance creates a plan assigned to HR; neither can retrieve plans merely through role.
    other = plan(client, reviewer_id="hr")
    db.get(Scenario, other["id"])  # real persisted object
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "finance"))
    grant.scope = "D12"
    db.commit()
    login()
    assert command(client, scenario, "submit").status_code == 403


def test_fx_and_late_source_correction_are_pinned(client, login):
    login()
    original = plan(client, base_snapshot_id=BASE_SNAPSHOT)
    corrected = plan(client)
    shock = plan(client, fx_set_id=FX_SHOCK_ID)
    old = forecast(client, original)["current"]["result"]
    new = forecast(client, corrected)["current"]["result"]
    fx = forecast(client, shock)["current"]["result"]
    assert old["inputs"]["snapshot_sha256"] != new["inputs"]["snapshot_sha256"]
    assert fx["inputs"]["fx_sha256"] != new["inputs"]["fx_sha256"]
    assert int(fx["annual"]["total_minor"]) > int(new["annual"]["total_minor"])
    assert forecast(client, corrected)["current"]["result"] == new


def test_worker_duplicate_delivery_and_manifest_crash(
    client, login, db, planning_worker, monkeypatch
):
    login()
    scenario = plan(client)
    assert planning_worker() == 1
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == scenario["id"]))
    event.state = "PENDING"
    db.commit()
    assert planning_worker() == 1
    assert db.scalar(select(func.count()).select_from(ScenarioVerification)) == 1
    assert db.scalar(select(func.count()).select_from(ConsumerReceipt)) == 1
    scenario = command(client, scenario, "submit").json()
    login("finance")
    scenario = command(client, scenario, "review").json()
    scenario = command(
        client,
        scenario,
        "decisions",
        {"decision": "approve", "reason": "Approved for crash recovery test"},
    ).json()

    class Crash(BaseException):
        pass

    def crash(session, *args):
        write_approved_manifest(session, *args)
        raise Crash()

    monkeypatch.setattr(worker, "write_approved_manifest", crash)
    with pytest.raises(Crash):
        planning_worker()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ScenarioManifest)) == 0
    monkeypatch.setattr(worker, "write_approved_manifest", write_approved_manifest)
    assert planning_worker() == 1
    approved_event = db.scalar(
        select(OutboxEvent).where(OutboxEvent.event_type == "scenario.approved.v1")
    )
    approved_event.state = "PENDING"
    db.commit()
    assert planning_worker() == 1
    assert db.scalar(select(func.count()).select_from(ScenarioManifest)) == 1
    assert db.scalar(select(func.count()).select_from(ConsumerReceipt)) == 2


def test_worker_outage_dead_letter_retry_and_source_binding(
    client, login, db, planning_worker, monkeypatch
):
    login()
    scenario = plan(client)

    def unavailable(*args):
        raise OSError("private provider details")

    monkeypatch.setattr(worker, "verify_revision", unavailable)
    for attempt in range(5):
        assert planning_worker() == 0
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == scenario["id"]))
        assert event.attempts == attempt + 1 and event.last_error == "JOB_EXECUTION_FAILED"
        event.next_attempt_at = now() - timedelta(seconds=1)
        db.commit()
    assert get(client, scenario)["failed_job"]
    assert command(client, scenario, "submit").status_code == 409
    monkeypatch.setattr(worker, "verify_revision", verify_revision)
    retry = command(client, scenario, "retry", {"reason": "Restored worker dependency"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["pending_job"] and retry.json()["version"] == 1
    assert planning_worker() == 1
    assert get(client, scenario)["verified"]
    revision_id = scenario["revisions"][0]["id"]
    from meridian.finance_engine import FinanceInputError

    with pytest.raises(FinanceInputError, match="EVENT_SOURCE_BINDING_MISMATCH"):
        verify_revision(db, scenario["id"], revision_id, uid())
    with pytest.raises(FinanceInputError, match="EVENT_REVISION_BINDING_MISMATCH"):
        verify_revision(db, uid(), revision_id, event.payload["source_batch_id"])


@pytest.mark.security
@pytest.mark.parametrize(
    "table,column,value",
    [
        ("scenario", "budget_minor", "1"),
        ("scenario", "fx_set_id", FX_SHOCK_ID),
        ("scenario_revision", "reason", "Changed sealed revision"),
        ("scenario_forecast", "sha256", "a" * 64),
        ("scenario_verification", "sha256", "a" * 64),
    ],
)
def test_database_seals_pins_and_financial_evidence(
    client, login, db, planning_worker, table, column, value
):
    login()
    scenario = plan(client)
    assert planning_worker() == 1
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(f"UPDATE {table} SET {column}=:value"), {"value": value})
    assert get(client, scenario)["version"] == 1


@pytest.mark.security
def test_direct_sql_cannot_skip_review_or_forge_verification(client, login, db):
    login()
    scenario = plan(client)
    revision = scenario["revisions"][0]["id"]
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("UPDATE scenario SET state='APPROVED',version=version+1"))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(ScenarioVerification(revision_id=revision, sha256="0" * 64))
        db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            ScenarioDecision(
                scenario_id=scenario["id"],
                revision_id=revision,
                actor_id="hr",
                expected_version=1,
                action="APPROVE",
                reason="Attempt a self approval",
                request_id=uid(),
            )
        )
        db.flush()
    assert get(client, scenario)["state"] == "DRAFT"


@pytest.mark.security
def test_unassigned_planner_cannot_read_fork_compare_or_download(client, login, db):
    login()
    scenario = plan(client)
    db.add(
        Principal(
            id="unrelated-finance",
            oidc_sub="unrelated-finance",
            display_name="Synthetic unrelated planner",
        )
    )
    db.flush()
    db.add(RoleAssignment(principal_id="unrelated-finance", role="finance", scope="*"))
    db.commit()
    login("unrelated-finance")
    assert client.get(ROOT).json()["items"] == []
    for path in ["", "/forecast", "/export", "/manifest"]:
        assert client.get(f"{ROOT}/{scenario['id']}{path}").status_code == 404
    assert (
        client.get(
            ROOT + "/compare", params=[("ids", scenario["id"]), ("ids", scenario["id"])]
        ).status_code
        == 404
    )
    assert (
        command(
            client,
            scenario,
            "branches",
            {
                "title": "Unauthorized fork",
                "reviewer_id": "hr",
                "reason": "Attempt cross object access",
            },
        ).status_code
        == 404
    )


def test_excess_budget_invalid_operation_and_blank_revision_roll_back(client, login, db):
    login()
    baseline = plan(client)
    capped = plan(client, budget_minor=baseline["annual_total_minor"])
    for op in [hire(), hire(effective_date="2028-01-02", annual_pay_minor="99999999999999999999")]:
        response = add(client, capped, op)
        assert response.status_code == 422, response.text
        db.commit()
        assert get(client, capped)["revision"] == 1
    response = command(
        client,
        capped,
        "branches",
        {"title": "Blank reason", "reviewer_id": "finance", "reason": "   "},
    )
    assert response.status_code == 422
    db.commit()
    assert db.scalar(select(func.count()).select_from(Scenario)) == 2
    assert db.scalar(select(func.count()).select_from(ScenarioRevision)) == 2
    assert not db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.verb == "scenario.operation_added")
    )
