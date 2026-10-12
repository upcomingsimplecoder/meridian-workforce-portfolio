"""Exact persisted replay, current authority, immutable attempts and corruption negatives."""

import copy
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from meridian import replay_service
from meridian.finance_models import CostPolicyVersion, FxRate, FxRateSet
from meridian.finance_repository import load_fx, load_policy
from meridian.models import (
    AuditEvent,
    ConsumerReceipt,
    OutboxEvent,
    Principal,
    RoleAssignment,
    now,
    uid,
)
from meridian.people_models import EmployeeVersion, OrgVersion, Snapshot, SourceBatch
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from meridian.policy import canonical_hash
from meridian.replay_models import ReplayArtifact, ReplayAttempt, ReplayCapsule
from meridian.replay_service import canonical_bytes, verify_packet
from meridian.scenario_models import ScenarioRevision
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_compensation_api import comp_worker as comp_worker
from test_compensation_api import drain, locked
from test_scenarios import add, command, hire, plan

ROOT = "/api/v1/replay"


def subject_scenario(client, login, db, comp_worker, **changes):
    login()
    scenario = plan(client, **changes)
    response = add(client, scenario, hire())
    assert response.status_code == 200, response.text
    scenario = response.json()
    drain(comp_worker, db)
    revision = db.scalar(
        select(ScenarioRevision).where(
            ScenarioRevision.scenario_id == scenario["id"], ScenarioRevision.number == 2
        )
    )
    return scenario, revision


def capture(client, subject_id, kind="SCENARIO", key=None):
    return client.post(
        ROOT + "/capsules",
        json={
            "kind": kind,
            "subject_id": subject_id,
            "reason": "Freeze the precise historical human decision and calculation",
        },
        headers={"Idempotency-Key": key or uid()},
    )


def get(client, capsule):
    response = client.get(ROOT + "/capsules/" + capsule["id"])
    assert response.status_code == 200, response.text
    return response.json()


def verify(client, capsule, key=None, expected=None):
    return client.post(
        ROOT + "/capsules/" + capsule["id"] + "/verify",
        json={"reason": "Reproduce the exact frozen source versions"},
        headers={
            "Idempotency-Key": key or uid(),
            "If-Match": str(expected or capsule["attempts"][0]["number"]),
        },
    )


def exported(client, capsule, attempt=None):
    return client.get(
        f"{ROOT}/capsules/{capsule['id']}/attempts/{attempt or capsule['attempts'][0]['id']}/export"
    )


def test_persisted_exact_leap_day_12_month_replay_and_same_key_receipt(
    client, login, db, comp_worker
):
    scenario, revision = subject_scenario(
        client, login, db, comp_worker, scope_orgs=[f"D{i:02d}" for i in range(1, 13)]
    )
    key = uid()
    response = capture(client, revision.id, key=key)
    assert response.status_code == 201, response.text
    capsule = response.json()
    assert capsule["aggregate"]["baseline"]["total_minor"] == "77714704176"
    assert capsule["aggregate"]["planned"]["total_minor"] == "77728752452"
    assert capsule["aggregate"]["delta_minor"] == "14048276"
    assert sum(int(row["delta_minor"]) for row in capsule["aggregate"]["months"]) == 14048276
    assert capsule["full_operations"][0]["annual_pay_minor"] == "12000000"
    assert capsule["attempts"][0]["expected_sha256"] == capsule["attempts"][0]["observed_sha256"]
    assert capsule["attempts"][0]["status"] == "MATCH" and capsule["attempts"][0]["export_ready"]
    assert capsule["integrity_issues"] == []
    assert {row["kind"] for row in capsule["timeline"]} == {"AUDIT", "OUTBOX"}
    assert capsule["timeline"] == sorted(
        capsule["timeline"], key=lambda row: (row["at"], row["kind"], row["id"])
    )
    assert capture(client, revision.id, key=key).json() == capsule
    assert db.scalar(select(func.count()).select_from(ReplayCapsule)) == 1
    assert db.scalar(select(func.count()).select_from(ReplayAttempt)) == 1
    assert get(client, capsule) == capsule
    response = exported(client, capsule)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert verify_packet(response.json())
    assert response.json()["evidence"]["aggregate"]["delta_minor"] == "14048276"
    assert (
        b"SYN-" not in response.content
        and b"annual_pay_minor" not in response.content
        and b"Synthetic leap day hire" not in response.content
    )
    login("finance")
    verified = verify(client, capsule)
    assert verified.status_code == 200, verified.text
    latest = verified.json()
    assert [row["number"] for row in latest["attempts"]] == [2, 1]
    assert latest["sha256"] == capsule["sha256"]
    assert latest["aggregate"] == capsule["aggregate"]
    assert exported(client, capsule).content == response.content


def test_locked_returned_award_replays_exact_amounts_and_human_hierarchy(
    client, login, db, comp_worker
):
    cycle, proposals = locked(client, login, db, comp_worker)
    proposal = proposals["SYN-000049"]
    response = capture(client, proposal["revision_id"], "AWARD")
    assert response.status_code == 201, response.text
    capsule = response.json()
    assert capsule["award"]["annualized_merit_minor"] == "100000"
    assert capsule["award"]["merit_first_year_minor"] == "66664"
    assert capsule["award"]["bonus_payable_minor"] == "100000"
    assert capsule["award"]["pool_charge_minor"] == "200000"
    assert [row["number"] for row in capsule["revision_history"]] == [1, 2, 3, 4]
    assert [row["role"] for row in capsule["approval_steps"]] == ["DIVISION", "HR", "FINANCE"]
    assert any(row["action"] == "RETURN" for row in capsule["decisions"])
    assert [
        row["ordinal"]
        for row in capsule["decisions"]
        if row["action"] == "APPROVE" and row["revision_id"] == proposal["revision_id"]
    ] == [1, 2, 3]
    assert capsule["attempts"][0]["status"] == "MATCH"
    login("auditor")
    redacted = get(client, capsule)
    assert redacted["redacted"] and redacted["award"] is None and redacted["full_operations"] == []
    assert all(row["reason"] is None for row in redacted["decisions"])
    assert "SYN-" not in json.dumps(redacted)
    choices = client.get(ROOT + "/choices", params={"kind": "AWARD"})
    assert choices.status_code == 200, choices.text
    assert {row["subject_id"] for row in choices.json()} == {
        row["revision_id"] for row in proposals.values()
    }
    obsolete = capsule["revision_history"][1]["id"]
    assert capture(client, obsolete, "AWARD").status_code == 404
    response = exported(client, capsule)
    assert response.status_code == 200 and verify_packet(response.json())
    assert b"SYN-" not in response.content and b"200000" not in response.content
    assert b"pool_charge_minor" not in response.content and b'reason"' not in response.content
    assert verify(client, redacted).json()["attempts"][0]["status"] == "MATCH"


@pytest.mark.security
@pytest.mark.parametrize("persona", ["manager-a", "manager-b", "executive", "steward", "approver"])
def test_unrelated_personas_cannot_read_capture_verify_export_or_audit(
    client, login, db, comp_worker, persona
):
    scenario, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    login(persona)
    for path in ("/choices", "/capsules", "/capsules/" + capsule["id"], "/audit"):
        response = client.get(ROOT + path)
        assert response.status_code == 403
        assert "SYN-" not in response.text and "minor" not in response.text
    assert capture(client, revision.id).status_code == 403
    assert verify(client, capsule).status_code == 403
    assert exported(client, capsule).status_code == 403
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.actor_id == persona, AuditEvent.permission_result == "DENY")
        )
        == 7
    )


def corrupt(db, table, sql, params):
    # Isolated rollback-only database simulates privileged accidental damage. Release
    # clients cannot disable triggers; the positive sealing tests below prove that.
    db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
    db.execute(text(sql), params)
    db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    db.commit()
    db.expire_all()


@pytest.mark.security
@pytest.mark.parametrize(
    "damage, expected",
    [
        ("FX_CHANGE", "FX"),
        ("MISSING_RATE", "FX"),
        ("AUDIT_HASH", "AUDIT_HASH_MISMATCH"),
        ("OUTBOX_PAYLOAD", "OUTBOX_HASH_MISMATCH"),
        ("MISSING_SOURCE", "SNAPSHOT_COUNT"),
        ("ENGINE", "UNSUPPORTED_PINNED_ENGINE_CODE"),
        ("POLICY", "POLICY"),
    ],
)
def test_dependency_damage_is_recorded_never_substituted_and_old_export_fails(
    client, login, db, comp_worker, monkeypatch, damage, expected
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    frozen = db.get(ReplayCapsule, capsule["id"])
    pins = frozen.document["inputs"]
    if damage == "FX_CHANGE":
        corrupt(
            db,
            "fx_rate",
            "UPDATE fx_rate SET rate_decimal=rate_decimal*2 WHERE set_id=:id AND source_currency='EUR'",
            {"id": pins["fx_set_id"]},
        )
    elif damage == "MISSING_RATE":
        corrupt(
            db,
            "fx_rate",
            "DELETE FROM fx_rate WHERE set_id=:id AND source_currency='EUR'",
            {"id": pins["fx_set_id"]},
        )
    elif damage == "AUDIT_HASH":
        anchor = next(row for row in frozen.document["anchors"] if row["kind"] == "AUDIT")
        corrupt(
            db,
            "audit_event",
            "UPDATE audit_event SET after_hash=:sha WHERE id=:id",
            {"id": anchor["id"], "sha": "0" * 64},
        )
    elif damage == "OUTBOX_PAYLOAD":
        anchor = next(row for row in frozen.document["anchors"] if row["kind"] == "OUTBOX")
        corrupt(
            db,
            "outbox_event",
            "UPDATE outbox_event SET payload='{}'::jsonb WHERE id=:id",
            {"id": anchor["id"]},
        )
    elif damage == "MISSING_SOURCE":
        source_id = db.scalar(
            text("SELECT id FROM employee_version WHERE snapshot_id=:id LIMIT 1"),
            {"id": capsule["snapshot_id"]},
        )
        corrupt(
            db, "employee_version", "DELETE FROM employee_version WHERE id=:id", {"id": source_id}
        )
    elif damage == "ENGINE":
        monkeypatch.setattr(replay_service, "engine_fingerprint", lambda _kind: "0" * 64)
    elif damage == "POLICY":
        corrupt(
            db,
            "cost_policy_version",
            "UPDATE cost_policy_version SET definition=jsonb_set(definition,'{variable_accrual_rate}','\"0.99\"'::jsonb) WHERE id=:id",
            {"id": pins["policy_id"]},
        )
    response = verify(client, capsule)
    assert response.status_code == 200, response.text
    failed = response.json()
    attempt = failed["attempts"][0]
    assert attempt["status"] in {"CORRUPT", "MISSING", "UNSUPPORTED"}
    assert any(expected in issue for issue in attempt["issues"]), attempt["issues"]
    assert not attempt["export_ready"] and attempt["observed_sha256"] is None
    assert failed["attempts"][1]["status"] == "MATCH"
    assert failed["sha256"] == capsule["sha256"] and failed["aggregate"] == capsule["aggregate"]
    assert exported(client, capsule).status_code == 503
    assert exported(client, failed).status_code == 503


def test_duplicate_delivery_and_operational_retry_do_not_change_event_identity(
    client, login, db, comp_worker
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    anchors = db.get(ReplayCapsule, capsule["id"]).document["anchors"]
    event_id = next(row["id"] for row in anchors if row["kind"] == "OUTBOX")
    receipts = db.scalar(
        select(func.count())
        .select_from(ConsumerReceipt)
        .where(ConsumerReceipt.event_id == event_id)
    )
    event = db.get(OutboxEvent, event_id)
    event.state = "PENDING"
    event.next_attempt_at = now()
    db.commit()
    drain(comp_worker, db)
    assert (
        db.scalar(
            select(func.count())
            .select_from(ConsumerReceipt)
            .where(ConsumerReceipt.event_id == event_id)
        )
        == receipts
    )
    rerun = verify(client, capsule)
    assert rerun.status_code == 200, rerun.text
    assert (
        rerun.json()["attempts"][0]["status"] == "MATCH"
        and rerun.json()["sha256"] == capsule["sha256"]
    )


def test_later_snapshot_and_new_fx_policy_versions_do_not_replace_pinned_inputs(
    client, login, db, comp_worker
):
    _, original = subject_scenario(client, login, db, comp_worker, base_snapshot_id=BASE_SNAPSHOT)
    capsule = capture(client, original.id).json()
    response = exported(client, capsule)
    _, corrected = subject_scenario(
        client, login, db, comp_worker, base_snapshot_id=CURRENT_SNAPSHOT
    )
    latest = capture(client, corrected.id).json()
    assert capsule["snapshot_id"] != latest["snapshot_id"]
    assert capsule["aggregate"] != latest["aggregate"]
    assert verify(client, capsule).json()["attempts"][0]["status"] == "MATCH"
    assert exported(client, capsule).content == response.content


def test_new_sealed_fx_and_cost_policy_versions_do_not_replace_old_pins(
    client, login, db, comp_worker
):
    _, original = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, original.id).json()
    response = exported(client, capsule)
    pins = db.get(ReplayCapsule, capsule["id"]).document["inputs"]
    old_fx = load_fx(db, pins["fx_set_id"])
    fx = replace(
        old_fx,
        id=uid(),
        rates=tuple((code, "1.25" if code == "EUR" else rate) for code, rate in old_fx.rates),
    )
    db.add(
        FxRateSet(
            id=fx.id,
            source_label="Later synthetic FX version",
            captured_at=now(),
            effective_start=fx.effective_start,
            effective_end=fx.effective_end,
            reporting_currency=fx.reporting_currency,
            state="BUILDING",
            sha256=fx.sha256,
        )
    )
    db.flush()
    db.add_all(
        [
            FxRate(
                set_id=fx.id,
                source_currency=code,
                quote_currency=fx.reporting_currency,
                rate_decimal=Decimal(rate),
            )
            for code, rate in fx.rates
        ]
    )
    db.flush()
    db.get(FxRateSet, fx.id).state = "SEALED"
    policy = replace(load_policy(db, pins["policy_id"]), id=uid(), variable_rate="0.06")
    db.add(
        CostPolicyVersion(
            id=policy.id,
            source_label="Later synthetic cost policy version",
            effective_date=policy.effective_date,
            definition=policy.document(),
            sha256=policy.sha256,
        )
    )
    db.commit()
    _, new_assumptions = subject_scenario(
        client, login, db, comp_worker, fx_set_id=fx.id, policy_id=policy.id
    )
    changed = capture(client, new_assumptions.id).json()
    assert changed["aggregate"] != capsule["aggregate"]
    assert verify(client, capsule).json()["attempts"][0]["status"] == "MATCH"
    assert exported(client, capsule).content == response.content


def test_branch_replays_exact_parent_revision_after_parent_is_revised(
    client, login, db, comp_worker
):
    parent, parent_revision = subject_scenario(client, login, db, comp_worker)
    response = command(
        client,
        parent,
        "branches",
        {
            "title": "Synthetic parent-pinned branch",
            "reviewer_id": "finance",
            "reason": "Preserve the exact chosen parent revision",
        },
    )
    assert response.status_code == 201, response.text
    branch = response.json()
    response = add(
        client,
        branch,
        hire(employee_id="SYN-900002", position_id="POS-900002", effective_date="2028-03-01"),
    )
    assert response.status_code == 200, response.text
    branch = response.json()
    drain(comp_worker, db)
    revision = db.scalar(
        select(ScenarioRevision).where(
            ScenarioRevision.scenario_id == branch["id"], ScenarioRevision.number == 2
        )
    )
    capsule = capture(client, revision.id).json()
    assert capsule["aggregate"]["baseline"]["total_minor"] == parent["annual_total_minor"]
    assert (
        next(row for row in capsule["pins"] if row["name"] == "Comparison baseline")["version"]
        == parent_revision.id
    )
    response = command(client, parent, "undo")
    assert response.status_code == 200
    drain(comp_worker, db)
    assert verify(client, capsule).json()["attempts"][0]["status"] == "MATCH"
    assert get(client, capsule)["aggregate"] == capsule["aggregate"]


@pytest.mark.security
def test_small_cohort_aggregate_and_redacted_export_suppress_monetary_values(
    client, login, db, comp_worker
):
    login()
    batch = SourceBatch(
        source_type="TEST",
        sha256=canonical_hash({"four_person_source": uid()}),
        state="PUBLISHED",
        accepted_count=4,
        rejected_count=0,
        watermark=date(2027, 6, 30),
        actor_id="hr",
        published_at=now(),
    )
    db.add(batch)
    db.flush()
    snapshot = Snapshot(
        source_batch_id=batch.id,
        title="Four-person synthetic suppression fixture",
        source_watermark=batch.watermark,
        sha256=batch.sha256,
        row_count=4,
        status="BUILDING",
    )
    db.add(snapshot)
    db.flush()
    db.add(
        OrgVersion(
            snapshot_id=snapshot.id,
            org_id="D01",
            name="Synthetic fixture organization",
            region="Americas",
            valid_from=date(2027, 1, 1),
        )
    )
    for employee_id in ("SYN-000001", "SYN-000013", "SYN-000025", "SYN-000037"):
        original = db.scalar(
            select(EmployeeVersion)
            .where(
                EmployeeVersion.snapshot_id == CURRENT_SNAPSHOT,
                EmployeeVersion.employee_id == employee_id,
            )
            .order_by(EmployeeVersion.valid_from)
        )
        values = {
            column.name: getattr(original, column.name)
            for column in EmployeeVersion.__table__.columns
        }
        db.add(
            EmployeeVersion(
                **(
                    values
                    | {
                        "id": uid(),
                        "snapshot_id": snapshot.id,
                        "source_batch_id": batch.id,
                        "org_id": "D01",
                        "region": "Americas",
                        "valid_to": None,
                        "manager_id": None,
                    }
                )
            )
        )
    db.flush()
    snapshot.status = "PUBLISHED"
    db.commit()
    scenario = plan(client, scope_orgs=["D01"], base_snapshot_id=snapshot.id)
    drain(comp_worker, db)
    revision = db.scalar(
        select(ScenarioRevision).where(
            ScenarioRevision.scenario_id == scenario["id"], ScenarioRevision.number == 1
        )
    )
    capsule = capture(client, revision.id).json()
    assert int(capsule["aggregate"]["planned"]["total_minor"]) > 0
    login("auditor")
    redacted = get(client, capsule)
    assert (
        redacted["aggregate"] is None
        and redacted["award"] is None
        and redacted["award_history"] == []
    )
    packet = exported(client, capsule)
    assert packet.status_code == 200 and packet.json()["evidence"]["aggregate"] is None


@pytest.mark.security
def test_corrupt_capsule_never_projects_untrusted_financial_values(client, login, db, comp_worker):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    corrupt(
        db,
        "replay_capsule",
        "UPDATE replay_capsule SET document=jsonb_set(document,'{aggregate,delta_minor}','\"99999999\"'::jsonb) WHERE id=:id",
        {"id": capsule["id"]},
    )
    for response in (
        client.get(ROOT + "/capsules/" + capsule["id"]),
        verify(client, capsule),
        exported(client, capsule),
    ):
        assert response.status_code == 503
        assert "99999999" not in response.text


@pytest.mark.security
def test_current_scope_revocation_applies_to_receipt_list_cursor_and_export(
    client, login, db, comp_worker
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    key = uid()
    capsule = capture(client, revision.id, key=key).json()
    grants = list(db.scalars(select(RoleAssignment).where(RoleAssignment.principal_id == "hr")))
    for grant in grants:
        grant.scope = "D01"
    db.commit()
    assert capture(client, revision.id, key=key).status_code == 404
    assert client.get(ROOT + "/capsules/" + capsule["id"]).status_code == 404
    assert verify(client, capsule).status_code == 404
    assert exported(client, capsule).status_code == 404
    assert client.get(ROOT + "/capsules").json()["items"] == []
    assert client.get(ROOT + "/choices").json() == []
    assert client.get(ROOT + "/capsules", params={"cursor": capsule["id"]}).status_code == 422
    assert client.get(ROOT + "/audit").status_code == 403


@pytest.mark.security
def test_named_scenario_ownership_current_pay_scope_and_export_binding(
    client, login, db, comp_worker
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    db.add(
        Principal(
            id="unrelated-finance",
            oidc_sub="unrelated-finance",
            display_name="Synthetic unrelated Finance",
            active=True,
        )
    )
    db.add(
        RoleAssignment(
            principal_id="unrelated-finance",
            role="finance",
            scope="*",
            effective_from=now() - timedelta(days=1),
        )
    )
    db.commit()
    login("unrelated-finance")
    assert capture(client, revision.id).status_code == 404
    assert client.get(ROOT + "/capsules/" + capsule["id"]).status_code == 404
    assert exported(client, capsule).status_code == 404
    assert client.get(ROOT + "/choices").json() == []
    login()
    other = capture(client, revision.id).json()
    assert exported(client, capsule, other["attempts"][0]["id"]).status_code == 404


def test_cas_and_same_key_verify_preserve_attempt_history(client, login, db, comp_worker):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    key = uid()
    response = verify(client, capsule, key)
    assert response.status_code == 200
    assert verify(client, capsule, key).json() == response.json()
    conflict = verify(client, capsule)
    assert conflict.status_code == 409
    assert db.scalar(select(func.count()).select_from(ReplayAttempt)) == 2
    assert db.scalar(select(func.count()).select_from(ReplayArtifact)) == 2


@pytest.mark.security
@pytest.mark.parametrize("table", ["replay_capsule", "replay_attempt", "replay_artifact"])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_replay_rows_cannot_be_mutated_even_by_direct_sql(
    client, login, db, comp_worker, table, operation
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                f"UPDATE {table} SET created_at=now()"
                if operation == "update"
                else f"DELETE FROM {table}"
            )
        )
    assert get(client, capsule)["attempts"][0]["status"] == "MATCH"


@pytest.mark.security
def test_packet_and_private_bytes_corruption_refuse_download(
    client, login, db, comp_worker, monkeypatch
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    packet = exported(client, capsule).json()
    assert verify_packet(packet)
    changed = copy.deepcopy(packet)
    changed["evidence"]["expected_sha256"] = "0" * 64
    assert not verify_packet(changed)
    assert not verify_packet(packet | {"extra": "untrusted"})
    monkeypatch.setattr(replay_service, "read_private", lambda _key: canonical_bytes(changed))
    assert exported(client, capsule).status_code == 503
    assert not get(client, capsule)["attempts"][0]["export_ready"]


def test_storage_failure_rolls_back_capsule_attempt_receipt_and_uses_safe_error(
    client, login, db, comp_worker, monkeypatch
):
    _, revision = subject_scenario(client, login, db, comp_worker)

    def unavailable(_raw):
        raise OSError("sensitive path must not be reflected")

    monkeypatch.setattr(replay_service, "put_private", unavailable)
    key = uid()
    response = capture(client, revision.id, key=key)
    assert response.status_code == 503 and "sensitive" not in response.text
    assert db.scalar(select(func.count()).select_from(ReplayCapsule)) == 0
    assert db.scalar(select(func.count()).select_from(ReplayAttempt)) == 0
    assert db.scalar(select(func.count()).select_from(ReplayArtifact)) == 0


def test_audit_cursor_filters_denials_and_never_returns_raw_person_or_route_id(
    client, login, db, comp_worker
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    login("executive")
    denial_response = exported(client, capsule)
    login("auditor")
    response = client.get(
        ROOT + "/audit",
        params={"permission": "DENY", "correlation_id": denial_response.headers["x-request-id"]},
    )
    assert response.status_code == 200
    assert (
        len(response.json()["items"]) == 1
        and response.json()["items"][0]["verb"] == "security.replay_denied"
    )
    assert (
        capsule["id"] not in response.text
        and "SYN-" not in response.text
        and "/api/" not in response.text
    )
    first = client.get(ROOT + "/audit", params={"limit": 2}).json()
    second = client.get(ROOT + "/audit", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert not {row["id"] for row in first["items"]}.intersection(
        row["id"] for row in second["items"]
    )
    assert client.get(ROOT + "/audit", params={"limit": 51}).status_code == 422
    assert client.get(ROOT + "/audit", params={"verb": "../../person"}).status_code == 422


@pytest.mark.security
def test_missing_signing_key_and_malformed_signature_fail_closed(
    client, login, db, comp_worker, monkeypatch
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    packet = exported(client, capsule).json()
    assert not verify_packet(packet | {"signature": []})

    def unavailable():
        raise OSError("private signing key path")

    monkeypatch.setattr(replay_service, "signing_key", unavailable)
    response = exported(client, capsule)
    assert response.status_code == 503 and response.json()["type"].endswith(
        "replay_signing_unavailable"
    )
    assert "private signing key path" not in response.text
    assert not get(client, capsule)["attempts"][0]["export_ready"]
    assert verify(client, capsule).status_code == 503
    assert db.scalar(select(func.count()).select_from(ReplayAttempt)) == 1


@pytest.mark.security
def test_subject_snapshot_binding_and_false_match_cannot_be_inserted(
    client, login, db, comp_worker
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    capsule = capture(client, revision.id).json()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            ReplayCapsule(
                kind="SCENARIO",
                subject_id=revision.id,
                snapshot_id=BASE_SNAPSHOT,
                owner_id="hr",
                document={},
                sha256="a" * 64,
                reason="Forged cross-source binding",
                request_id=uid(),
            )
        )
        db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            ReplayAttempt(
                capsule_id=capsule["id"],
                number=2,
                actor_id="hr",
                status="MATCH",
                expected_sha256=capsule["attempts"][0]["expected_sha256"],
                observed_sha256="0" * 64,
                issues=[],
                reason="Forged matching result",
                request_id=uid(),
            )
        )
        db.flush()
    assert get(client, capsule)["attempts"][0]["status"] == "MATCH"


def test_blank_reason_and_unknown_capture_fields_are_rejected_before_persistence(
    client, login, db, comp_worker
):
    _, revision = subject_scenario(client, login, db, comp_worker)
    for extra in ({"reason": "   "}, {"actor_id": "auditor"}):
        response = client.post(
            ROOT + "/capsules",
            json={"kind": "SCENARIO", "subject_id": revision.id, "reason": "Valid capture purpose"}
            | extra,
            headers={"Idempotency-Key": uid()},
        )
        assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(ReplayCapsule)) == 0
