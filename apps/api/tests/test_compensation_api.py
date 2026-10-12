"""Actual HTTP/SQL compensation journeys, authorization and transactional jobs."""

import copy
from datetime import timedelta

import pytest
from meridian import object_store
from meridian.compensation_exports import verify_manifest, write_export
from meridian.compensation_models import (
    CompCycle,
    CompDecision,
    CompExportArtifact,
    CompProposalRevision,
    CompReconciliation,
)
from meridian.models import AuditEvent, ConsumerReceipt, OutboxEvent, RoleAssignment, now, uid
from meridian.people_models import PrincipalEmployee
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from test_compensation_workflow import configuration

from apps.worker import run as worker

ROOT = "/api/v1/compensation"


@pytest.fixture
def comp_worker(db, monkeypatch, tmp_path):
    from meridian.config import settings

    monkeypatch.setattr(settings(), "local_data_dir", str(tmp_path))
    object_store.ensure_key()
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


def command(client, record, route, body=None, *, proposal=False, **headers):
    return client.post(
        f"{ROOT}/{'proposals' if proposal else 'cycles'}/{record['id']}/{route}",
        json=body or {"reason": "Synthetic human decision"},
        headers={"If-Match": str(record["version"]), "Idempotency-Key": uid()} | headers,
    )


def ok(response):
    # No export/session responses go through this assertion: those contain tokens.
    assert response.status_code == 200, response.text
    return response.json()


def create(client, **changes):
    return client.post(
        ROOT + "/cycles", json=configuration(**changes), headers={"Idempotency-Key": uid()}
    )


def launched(client, login):
    login()
    cycle = ok(create(client))
    return ok(command(client, cycle, "launch"))


@pytest.mark.security
def test_creation_receipt_rechecks_all_configured_scopes_after_partial_revocation(
    client, login, db
):
    login()
    db.add(
        RoleAssignment(
            principal_id="approver",
            role="approver",
            scope="D02",
            effective_from=now() - timedelta(days=1),
        )
    )
    db.commit()
    body = configuration(
        selected_ids=["SYN-000025", "SYN-000037"],
        pools=[
            {
                "manager_id": manager,
                "org_id": org,
                "currency": "USD",
                "budget_minor": "2000000",
                "holdback_minor": "100000",
            }
            for manager, org in (("manager-a", "D01"), ("manager-b", "D02"))
        ],
        approvals=[
            {
                "org_id": org,
                "division_approver_id": "approver",
                "hr_signoff_id": "hr",
                "finance_signoff_id": "finance",
            }
            for org in ("D01", "D02")
        ],
    )
    headers = {"Idempotency-Key": uid()}
    original = ok(client.post(ROOT + "/cycles", json=body, headers=headers))
    assert original["configured_population_count"] == 2
    assert {pool["org_id"] for pool in original["pools"]} == {"D01", "D02"}
    assert ok(client.post(ROOT + "/cycles", json=body, headers=headers)) == original
    cycle = ok(command(client, original, "launch"))
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "hr"))
    grant.scope = "D01"
    db.commit()
    scoped = ok(client.get(f"{ROOT}/cycles/{cycle['id']}"))
    assert scoped["configured_population_count"] is None
    assert {pool["org_id"] for pool in scoped["pools"]} == {"D01"}
    replayed = client.post(ROOT + "/cycles", json=body, headers=headers)
    assert replayed.status_code == 403
    assert replayed.json()["type"] == "urn:meridian:problem:comp_controller_required"
    assert "pools" not in replayed.json()
    assert "configured_population_count" not in replayed.json()
    assert db.scalar(select(func.count()).select_from(CompCycle)) == 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.verb == "security.comp_controller_required")
        )
        == 1
    )
    grant.scope = "*"
    db.commit()
    assert ok(client.post(ROOT + "/cycles", json=body, headers=headers)) == original


def roster(client, cycle):
    return ok(client.get(f"{ROOT}/cycles/{cycle['id']}/proposals"))["items"]


def detail(client, proposal):
    return ok(client.get(f"{ROOT}/proposals/{proposal['id']}"))


def cycle_detail(client, cycle):
    return ok(client.get(f"{ROOT}/cycles/{cycle['id']}"))


def drain(comp_worker, db):
    for _ in range(100):
        if not db.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.state == "PENDING", OutboxEvent.next_attempt_at <= now()
            )
        ):
            return
        assert comp_worker() == 1
    raise AssertionError("Unexpected unbounded outbox work")


def award(client, proposal, merit="120000", bonus="100000", **headers):
    return command(
        client,
        proposal,
        "award",
        {
            "merit_minor": merit,
            "bonus_minor": bonus,
            "comment": "Synthetic proposal rationale",
            "reason": "Save a reasoned award",
        },
        proposal=True,
        **headers,
    )


def decision(client, proposal, action="approve", exceptions=None, **changes):
    return command(
        client,
        proposal,
        "decisions",
        {
            "revision_id": proposal["revision_id"],
            "decision": action,
            "reason": "Synthetic independent review",
            "acknowledged_exceptions": exceptions or [],
        }
        | changes,
        proposal=True,
    )


def ready_for_finance(client, login, db, comp_worker, *, returned=True):
    cycle = launched(client, login)
    proposals = {}
    for manager in ("manager-a", "manager-b"):
        login(manager)
        rows = roster(client, cycle)
        assert len(rows) == (1 if manager == "manager-a" else 2)
        scoped = cycle_detail(client, cycle)
        assert scoped["configured_population_count"] is None
        assert {pool["manager_id"] for pool in scoped["pools"]} == {manager}
        for row in rows:
            proposals[row["employee_id"]] = ok(
                award(client, row, "1000000" if manager == "manager-a" else "120000")
            )
    drain(comp_worker, db)
    for employee, proposal in proposals.items():
        login(proposal["proposer_id"])
        proposal = detail(client, proposal)
        assert proposal["verified"]
        proposals[employee] = ok(command(client, proposal, "submit", proposal=True))
    login("approver")
    for employee, proposal in proposals.items():
        proposal = ok(
            command(
                client,
                detail(client, proposal),
                "review",
                {
                    "revision_id": proposal["revision_id"],
                    "reason": "Review the current saved revision",
                },
                proposal=True,
            )
        )
        if employee == "SYN-000025":
            assert decision(client, proposal).status_code == 422
        proposals[employee] = ok(
            decision(
                client,
                proposal,
                exceptions=["MERIT_GUIDELINE_EXCEPTION"] if employee == "SYN-000025" else [],
            )
        )
    if returned:
        login()
        proposal = proposals["SYN-000049"]
        old_revision = proposal["revision_id"]
        returned_proposal = ok(decision(client, proposal, "return"))
        assert returned_proposal["state"] == "RETURNED" and returned_proposal["revision"] == 3
        assert not any(step["approved"] for step in returned_proposal["steps"])
        login("manager-b")
        proposal = ok(award(client, returned_proposal, "100000"))
        assert proposal["revision"] == 4
        historical = ok(client.get(f"{ROOT}/proposals/{proposal['id']}", params={"revision": 2}))
        assert (
            historical["revision_id"] == old_revision
            and historical["award"]["merit_minor"] == "120000"
        )
        drain(comp_worker, db)
        proposal = ok(command(client, detail(client, proposal), "submit", proposal=True))
        login("approver")
        proposal = ok(
            command(
                client,
                proposal,
                "review",
                {"revision_id": proposal["revision_id"], "reason": "Review corrected revision"},
                proposal=True,
            )
        )
        proposals["SYN-000049"] = ok(decision(client, proposal))
    login()
    for employee, proposal in proposals.items():
        proposals[employee] = ok(decision(client, detail(client, proposal)))
    cycle = cycle_detail(client, cycle)
    assert cycle["state"] == "FINAL_APPROVAL" and cycle["current_round_id"]
    return cycle, proposals


def locked(client, login, db, comp_worker):
    cycle, proposals = ready_for_finance(client, login, db, comp_worker)
    login("finance")
    for employee, proposal in proposals.items():
        proposal = detail(client, proposal)
        if employee != "SYN-000025":
            assert decision(client, proposal).status_code == 422
        proposals[employee] = ok(
            decision(
                client,
                proposal,
                exceptions=["POOL_HOLDBACK_RELEASE"] if employee != "SYN-000025" else [],
            )
        )
    login()
    cycle = ok(command(client, cycle_detail(client, cycle), "lock"))
    assert cycle["state"] == "LOCKED"
    return cycle, proposals


def test_http_return_resubmit_three_signatures_lock_export_reconcile(
    client, login, db, comp_worker
):
    cycle, proposals = locked(client, login, db, comp_worker)
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 409
    for proposal in proposals.values():
        login(proposal["proposer_id"])
        assert award(client, detail(client, proposal)).status_code == 409
        assert command(client, detail(client, proposal), "submit", proposal=True).status_code == 409
    drain(comp_worker, db)
    login()
    exported = client.get(f"{ROOT}/cycles/{cycle['id']}/export")
    assert exported.status_code == 200
    document = exported.json()
    assert verify_manifest(document["document"])
    assert document["row_count"] == 3
    assert document["document"]["manifest"]["totals_by_currency"] == {
        "USD": {
            "annualized_merit_minor": "1220000",
            "merit_first_year_minor": "813328",
            "bonus_payable_minor": "300000",
            "pool_charge_minor": "1520000",
        }
    }
    downloaded = client.get(
        f"{ROOT}/cycles/{cycle['id']}/download", params={"token": document["download_token"]}
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["cache-control"] == "no-store"
    assert len(downloaded.content.splitlines()) == 4
    assert b"Synthetic proposal rationale" not in downloaded.content
    assert b"SYN-000049,D01,CC-001,USD,2,2028-05-01," in downloaded.content
    # Complete independently assembled golden: exact column order, amounts, FTE,
    # policy/source pins and returned proposal revision. Not generated by csv_for.
    from meridian.compensation_exports import CSV_COLUMNS
    from meridian.compensation_seed import GUIDELINE_ID
    from meridian.finance_seed import FX_BASE_ID
    from meridian.people_seed import CURRENT_SNAPSHOT

    expected_rows = [
        ("SYN-000025", "12300000", "1000000", "13300000", "666664", "1100000", "2"),
        ("SYN-000049", "9700000", "100000", "9800000", "66664", "200000", "4"),
        ("SYN-000073", "17700000", "120000", "17820000", "80000", "220000", "2"),
    ]
    expected_csv = ",".join(CSV_COLUMNS) + "\n"
    for employee, base, merit, new_base, first_year, charge, revision in expected_rows:
        expected_csv += (
            ",".join(
                [
                    employee,
                    "D01",
                    "CC-001",
                    "USD",
                    "2",
                    "2028-05-01",
                    base,
                    merit,
                    new_base,
                    "1",
                    merit,
                    first_year,
                    "2029-01-15",
                    "100000",
                    "100000",
                    charge,
                    revision,
                    CURRENT_SNAPSHOT,
                    GUIDELINE_ID,
                    FX_BASE_ID,
                ]
            )
            + "\n"
        )
    assert downloaded.content == expected_csv.encode()
    # A fresh same-person session and another authorized principal cannot reuse the link.
    login()
    assert (
        client.get(
            f"{ROOT}/cycles/{cycle['id']}/download", params={"token": document["download_token"]}
        ).status_code
        == 403
    )
    login("finance")
    assert (
        client.get(
            f"{ROOT}/cycles/{cycle['id']}/download", params={"token": document["download_token"]}
        ).status_code
        == 403
    )
    login()
    cycle = ok(command(client, cycle_detail(client, cycle), "reconcile"))
    assert cycle["state"] == "RECONCILED"
    reconciled = db.get(CompReconciliation, cycle["id"])
    assert reconciled is not None
    assert reconciled.document["warehouse_validation"] == (
        "UNVERIFIED; this source comparison does not execute analytics or Snowflake"
    )
    assert reconciled.document["byte_for_byte_source_match"] is True
    login("auditor")
    evidence = ok(client.get(f"{ROOT}/cycles/{cycle['id']}/evidence"))
    assert evidence["signature_verified"] and evidence["reconciled"]
    assert evidence["totals_by_currency"]["USD"]["pool_charge_minor"] == "1520000"
    for forbidden in ("SYN-", "manager-a", "manager-b", "comment", "base_pay", "rationale"):
        assert forbidden not in str(evidence)
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 403
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/proposals").status_code == 403


@pytest.mark.security
@pytest.mark.parametrize("persona", ["executive", "auditor", "steward"])
def test_raw_compensation_routes_fail_closed(client, login, persona):
    cycle = launched(client, login)
    proposal = roster(client, cycle)[0]
    login(persona)
    for route in (
        "/options",
        "/cycles",
        f"/cycles/{cycle['id']}",
        f"/cycles/{cycle['id']}/proposals",
        f"/cycles/{cycle['id']}/eligibility",
        f"/cycles/{cycle['id']}/export",
        f"/cycles/{cycle['id']}/download?token=forged",
        f"/proposals/{proposal['id']}",
    ):
        assert client.get(ROOT + route).status_code == 403
    assert create(client).status_code == 403
    for route in ("launch", "lock", "reconcile", "retry-export"):
        assert command(client, cycle, route).status_code == 403
    assert award(client, proposal).status_code == 403
    assert command(client, proposal, "submit", proposal=True).status_code == 403
    if persona != "auditor":
        assert client.get(ROOT + "/evidence").status_code == 403


@pytest.mark.security
def test_manager_isolated_roster_pay_pools_and_self_approval(client, login, db):
    cycle = launched(client, login)
    rows = roster(client, cycle)
    login("manager-a")
    own = roster(client, cycle)
    assert len(own) == 1 and own[0]["employee_id"] == "SYN-000025"
    for row in rows:
        if row["proposer_id"] == "manager-b":
            assert client.get(f"{ROOT}/proposals/{row['id']}").status_code == 404
            assert award(client, row).status_code == 404
    assert decision(client, own[0]).status_code == 403
    assert (
        command(
            client,
            own[0],
            "review",
            {"revision_id": own[0]["revision_id"], "reason": "Forbidden self review"},
            proposal=True,
        ).status_code
        == 403
    )
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 403
    # Changing the trusted root removes current access; frozen ownership cannot elevate it.
    db.get(PrincipalEmployee, "manager-a").employee_id = "SYN-000003"
    db.commit()
    assert client.get(f"{ROOT}/cycles/{cycle['id']}").status_code == 404
    assert award(client, own[0]).status_code == 404


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    [
        {"owner_id": "manager-a"},
        {"state": "LOCKED"},
        {"authz_policy_id": "authz-v1"},
        {"title": "   "},
        {"reason": "   "},
        {"selected_ids": ["SYN-000025", "SYN-000025"]},
    ],
)
def test_invalid_configuration_rolls_back(client, login, db, change):
    login()
    before = db.scalar(select(func.count()).select_from(CompCycle))
    assert create(client, **change).status_code == 422
    assert db.scalar(select(func.count()).select_from(CompCycle)) == before


@pytest.mark.security
def test_configuration_sod_scope_and_revoked_assignees(client, login, db):
    login()
    body = configuration()
    body["approvals"][0]["division_approver_id"] = "manager-a"
    denied = create(client, **body)
    assert denied.status_code == 403
    events = list(
        db.scalars(
            select(AuditEvent).where(AuditEvent.request_id == denied.headers["X-Request-ID"])
        )
    )
    assert len(events) == 1 and events[0].permission_result == "DENY"
    body = configuration()
    body["approvals"][0]["finance_signoff_id"] = "hr"
    assert create(client, **body).status_code == 403
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "hr"))
    grant.scope = "D02"
    db.commit()
    assert create(client).status_code == 403


def test_idempotency_versions_budget_and_unverified_submit(client, login, db):
    cycle = launched(client, login)
    login("manager-b")
    proposal = roster(client, cycle)[0]
    count = db.scalar(select(func.count()).select_from(CompProposalRevision))
    assert award(client, proposal, "499999", "100001").status_code == 422
    assert db.scalar(select(func.count()).select_from(CompProposalRevision)) == count
    key = uid()
    saved = ok(award(client, proposal, **{"Idempotency-Key": key}))
    assert ok(award(client, proposal, **{"Idempotency-Key": key})) == saved
    assert award(client, proposal, "100001", **{"Idempotency-Key": key}).status_code == 409
    assert award(client, proposal).status_code == 409
    assert command(client, saved, "submit", proposal=True).status_code == 409
    client.headers["X-CSRF-Token"] = "forged"
    assert award(client, saved).status_code == 403


def test_worker_duplicate_and_export_after_file_crash(client, login, db, comp_worker, monkeypatch):
    cycle, _ = locked(client, login, db, comp_worker)
    drain_other = db.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == cycle["id"],
            OutboxEvent.event_type == "comp.cycle_locked.v1",
        )
    )
    # Process prior submit events before the export fault.
    while (
        db.scalar(
            select(OutboxEvent.id)
            .where(OutboxEvent.state == "PENDING")
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .limit(1)
        )
        != drain_other.id
    ):
        assert comp_worker() == 1

    def crash(*args, **kwargs):
        write_export(*args, **kwargs)
        raise KeyboardInterrupt("Simulated post-file pre-ack worker crash")

    monkeypatch.setattr(worker, "write_export", crash)
    with pytest.raises(KeyboardInterrupt):
        comp_worker()
    db.expire_all()
    assert db.get(CompExportArtifact, cycle["id"]) is None
    assert db.get(OutboxEvent, drain_other.id).state == "PENDING"
    monkeypatch.setattr(worker, "write_export", write_export)
    assert comp_worker() == 1
    artifact = db.get(CompExportArtifact, cycle["id"])
    fingerprint = artifact.sha256
    event = db.get(OutboxEvent, drain_other.id)
    event.state = "PENDING"
    db.commit()
    assert comp_worker() == 1
    assert db.get(CompExportArtifact, cycle["id"]).sha256 == fingerprint
    assert db.get(ConsumerReceipt, ("compensation-v1", event.id)) is not None
    assert (
        db.scalar(
            select(func.count())
            .select_from(CompExportArtifact)
            .where(CompExportArtifact.cycle_id == cycle["id"])
        )
        == 1
    )
    # Signature tampering cannot produce an accepted immutable manifest.
    changed = copy.deepcopy(artifact.document)
    changed["manifest"]["row_count"] = 999
    assert not verify_manifest(changed)


def test_worker_storage_outage_dead_letter_explicit_retry(
    client, login, db, comp_worker, monkeypatch
):
    cycle, _ = locked(client, login, db, comp_worker)
    event = db.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == cycle["id"],
            OutboxEvent.event_type == "comp.cycle_locked.v1",
        )
    )
    while (
        db.scalar(
            select(OutboxEvent.id)
            .where(OutboxEvent.state == "PENDING")
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .limit(1)
        )
        != event.id
    ):
        assert comp_worker() == 1

    def unavailable(*args, **kwargs):
        raise OSError("Synthetic unavailable private storage")

    monkeypatch.setattr(worker, "write_export", unavailable)
    for attempt in range(1, 6):
        assert comp_worker() == 0
        event = db.get(OutboxEvent, event.id)
        assert event.attempts == attempt
        assert event.last_error == "JOB_EXECUTION_FAILED"
        event.next_attempt_at = now() - timedelta(seconds=1)
        db.commit()
    assert event.state == "DEAD"
    assert cycle_detail(client, cycle)["state"] == "FAILED_EXPORT"
    assert db.get(CompExportArtifact, cycle["id"]) is None
    monkeypatch.setattr(worker, "write_export", write_export)
    cycle = ok(command(client, cycle_detail(client, cycle), "retry-export"))
    assert cycle["state"] == "LOCKED"
    assert comp_worker() == 1
    assert cycle_detail(client, cycle)["state"] == "EXPORTED"
    assert command(client, cycle_detail(client, cycle), "retry-export").status_code == 409


@pytest.mark.security
def test_corrupt_export_bytes_never_mint_or_serve_download_link(client, login, db, comp_worker):
    cycle, _ = locked(client, login, db, comp_worker)
    drain(comp_worker, db)
    exported = client.get(f"{ROOT}/cycles/{cycle['id']}/export")
    assert exported.status_code == 200
    token = exported.json()["download_token"]
    artifact = db.get(CompExportArtifact, cycle["id"])
    path = object_store.object_path(artifact.csv_key)
    original = path.read_bytes()
    path.write_bytes(b"Synthetic corrupt export bytes")
    unavailable = client.get(f"{ROOT}/cycles/{cycle['id']}/export")
    assert unavailable.status_code == 503
    assert "download_token" not in unavailable.json()
    assert (
        client.get(f"{ROOT}/cycles/{cycle['id']}/download", params={"token": token}).status_code
        == 503
    )
    path.write_bytes(original)
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 200
    login("finance")
    current_export = client.get(f"{ROOT}/cycles/{cycle['id']}/export")
    assert current_export.status_code == 200
    current_token = current_export.json()["download_token"]
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "finance"))
    grant.effective_to = now() - timedelta(seconds=1)
    db.commit()
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 403
    assert (
        client.get(
            f"{ROOT}/cycles/{cycle['id']}/download", params={"token": current_token}
        ).status_code
        == 403
    )


@pytest.mark.security
def test_serial_reviewer_cannot_skip_step_or_choose_old_revision(client, login, db, comp_worker):
    cycle = launched(client, login)
    login("manager-a")
    proposal = ok(award(client, roster(client, cycle)[0]))
    drain(comp_worker, db)
    proposal = ok(command(client, detail(client, proposal), "submit", proposal=True))
    login("finance")
    assert decision(client, proposal).status_code == 403

    login("approver")
    assert decision(client, proposal).status_code == 409
    proposal = ok(
        command(
            client,
            proposal,
            "review",
            {"revision_id": proposal["revision_id"], "reason": "Start named review"},
            proposal=True,
        )
    )
    count = db.scalar(select(func.count()).select_from(CompDecision))
    assert decision(client, proposal, revision_id=uid()).status_code == 409
    assert db.scalar(select(func.count()).select_from(CompDecision)) == count
    proposal = ok(decision(client, proposal))
    assert decision(client, proposal).status_code == 403


def test_pinned_old_cycle_preserves_manager_chain_after_late_source_correction(client, login):
    from meridian.people_seed import BASE_SNAPSHOT

    login()
    old = ok(
        create(
            client,
            snapshot_id=BASE_SNAPSHOT,
            pools=[
                {
                    "manager_id": "manager-a",
                    "org_id": "D01",
                    "currency": "USD",
                    "budget_minor": "5000000",
                    "holdback_minor": "0",
                }
            ],
        )
    )
    old = ok(command(client, old, "launch"))
    fingerprint = old["inputs_sha256"]
    corrected = launched(client, login)
    login("manager-a")
    assert {row["employee_id"] for row in roster(client, old)} == {
        "SYN-000025",
        "SYN-000049",
        "SYN-000073",
    }
    assert {row["employee_id"] for row in roster(client, corrected)} == {"SYN-000025"}
    assert cycle_detail(client, old)["inputs_sha256"] == fingerprint
    login("manager-b")
    assert client.get(f"{ROOT}/cycles/{old['id']}").status_code == 404
    assert {row["employee_id"] for row in roster(client, corrected)} == {"SYN-000049", "SYN-000073"}


def test_duplicate_submission_preserves_one_decision_and_outbox_event(
    client, login, db, comp_worker
):
    cycle = launched(client, login)
    login("manager-a")
    proposal = ok(award(client, roster(client, cycle)[0]))
    drain(comp_worker, db)
    proposal = detail(client, proposal)
    before = db.scalar(select(func.count()).select_from(CompDecision))
    key = uid()
    submitted = ok(command(client, proposal, "submit", proposal=True, **{"Idempotency-Key": key}))
    replayed = ok(command(client, proposal, "submit", proposal=True, **{"Idempotency-Key": key}))
    assert replayed == submitted
    assert db.scalar(select(func.count()).select_from(CompDecision)) == before + 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == proposal["id"],
                OutboxEvent.event_type == "comp.proposal_submitted.v1",
            )
        )
        == 1
    )
    assert command(client, proposal, "submit", proposal=True).status_code == 409
