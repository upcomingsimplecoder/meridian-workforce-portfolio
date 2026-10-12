"""Persisted HTTP review, exfiltration negatives and independently retried jobs."""

import copy
import json
from datetime import timedelta

import pytest
from meridian import object_store, talent_exports
from meridian.models import AuditEvent, ConsumerReceipt, OutboxEvent, RoleAssignment, now, uid
from meridian.people_models import PrincipalEmployee
from meridian.people_seed import CURRENT_SNAPSHOT
from meridian.talent_models import ReviewPacket, TalentExport
from meridian.talent_seed import TALENT_POLICY_ID
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from apps.worker import run as worker

ROOT = "/api/v1/talent"


@pytest.fixture
def talent_worker(db, monkeypatch, tmp_path):
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


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def configuration(**changes):
    return {
        "title": "Synthetic narrative calibration",
        "snapshot_id": CURRENT_SNAPSHOT,
        "policy_id": TALENT_POLICY_ID,
        "as_of": "2028-03-31",
        "due_date": "2028-06-30",
        "assignments": [
            {
                "employee_id": employee,
                "author_id": author,
                "calibrator_id": "approver",
                "hr_signer_id": "hr",
            }
            for employee, author in [("SYN-000025", "manager-a"), ("SYN-000049", "manager-b")]
        ],
        "reason": "Freeze fully synthetic context and independent human reviewers",
    } | changes


def command(client, row, route, body=None, *, cycle=False, **headers):
    return client.post(
        f"{ROOT}/{'cycles' if cycle else 'packets'}/{row['id']}/{route}",
        json=body or {"reason": "Synthetic independent human action"},
        headers={"If-Match": str(row["version"]), "Idempotency-Key": uid()} | headers,
    )


def launched(client, login):
    login()
    cycle = ok(
        client.post(ROOT + "/cycles", json=configuration(), headers={"Idempotency-Key": uid()})
    )
    return ok(command(client, cycle, "launch", cycle=True))


def roster(client, cycle):
    return ok(client.get(f"{ROOT}/cycles/{cycle['id']}/packets"))["items"]


def detail(client, packet):
    return ok(client.get(f"{ROOT}/packets/{packet['id']}"))


def cycle_detail(client, cycle):
    return ok(client.get(f"{ROOT}/cycles/{cycle['id']}"))


def review(packet, suffix=""):
    return {
        "accomplishments": "Synthetic observation: completed the documented demonstration "
        + suffix,
        "collaboration": "Synthetic observation: explained a handoff to a colleague " + suffix,
        "development": "Synthetic observation: practice stronger source citations " + suffix,
        "evidence": [
            {
                "kind": "WORKFORCE_CONTEXT",
                "source_version_id": packet["source_version_id"],
                "observation": "Synthetic human observation bound to this exact source context",
            }
        ],
    }


def save(client, packet, suffix="", **headers):
    return command(
        client,
        packet,
        "review",
        {"review": review(packet, suffix), "reason": "Save human-authored observations"},
        **headers,
    )


def approved(client, login, packet, *, returned=False, calibrate=True):
    login(packet["author_id"])
    packet = detail(client, packet)
    packet = ok(save(client, packet))
    packet = ok(command(client, packet, "submit"))
    login("approver")
    packet = ok(command(client, detail(client, packet), "start-calibration"))
    if calibrate:
        packet = ok(
            command(
                client,
                packet,
                "calibration",
                {
                    "review": review(packet, "clarified in calibration"),
                    "reason": "Clarify observable source evidence",
                },
            )
        )
    packet = ok(
        command(
            client,
            packet,
            "ack-calibration",
            {"reason": "Independent human reviewed this exact revision", "acknowledgement": True},
        )
    )
    login()
    packet = detail(client, packet)
    if returned:
        old_hash = packet["revision_sha256"]
        packet = ok(command(client, packet, "return"))
        assert packet["state"] == "RETURNED"
        login(packet["author_id"])
        packet = ok(save(client, detail(client, packet), "corrected after HR return"))
        assert packet["revision_sha256"] != old_hash
        packet = ok(command(client, packet, "submit"))
        login("approver")
        packet = ok(command(client, detail(client, packet), "start-calibration"))
        packet = ok(
            command(
                client,
                packet,
                "ack-calibration",
                {"reason": "Review corrected current revision", "acknowledgement": True},
            )
        )
        login()
        packet = detail(client, packet)
    return ok(
        command(
            client,
            packet,
            "approve",
            {"reason": "Independent HR signs this exact revision", "acknowledgement": True},
        )
    )


def locked(client, login):
    cycle = launched(client, login)
    rows = roster(client, cycle)
    for row in rows:
        approved(client, login, row)
    login()
    return ok(command(client, cycle_detail(client, cycle), "lock", cycle=True)), rows


def drain(talent_worker, db):
    for _ in range(100):
        if not db.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.state == "PENDING", OutboxEvent.next_attempt_at <= now()
            )
        ):
            return
        assert talent_worker() == 1
    raise AssertionError("Outbox did not drain within its expected work budget")


def export_bytes(client, cycle, number=None):
    response = client.get(
        f"{ROOT}/cycles/{cycle['id']}/export", params={"lock_number": number} if number else {}
    )
    # No assertions print a token-bearing manifest on failure.
    assert response.status_code == 200
    result = response.json()
    download = client.get(result["download_path"])
    assert download.status_code == 200
    return result, download.content


def test_http_return_calibrate_lock_reopen_history_and_exact_export(
    client, login, db, talent_worker
):
    cycle = launched(client, login)
    rows = roster(client, cycle)
    packet = next(row for row in rows if row["author_id"] == "manager-a")
    login("manager-a")
    packet = detail(client, packet)
    packet = ok(
        command(
            client,
            packet,
            "comments",
            {"body": "Synthetic evidence clarification", "reason": "Record a scoped comment"},
        )
    )
    packet = approved(client, login, packet, returned=True)
    assert packet["revision"] == 4
    assert len(packet["calibrations"]) == 1
    assert len([d for d in packet["decisions"] if d["action"] == "ACK_CALIBRATION"]) == 2
    peer = next(row for row in rows if row["author_id"] == "manager-b")
    approved(client, login, peer)
    login()
    cycle = ok(command(client, cycle_detail(client, cycle), "lock", cycle=True))
    assert cycle["state"] == "LOCKED" and cycle["lock_number"] == 1
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 409
    drain(talent_worker, db)
    manifest, original = export_bytes(client, cycle)
    assert manifest["current"] and manifest["row_count"] == 2
    document = json.loads(original)
    assert talent_exports.signature_valid(document)
    assert [r["employee_id"] for r in document["manifest"]["rows"]] == ["SYN-000025", "SYN-000049"]
    assert all(len(r["signatures"]) == 2 for r in document["manifest"]["rows"])
    assert len(document["manifest"]["rows"][0]["comments"]) == 1
    cycle = ok(
        command(
            client,
            cycle_detail(client, cycle),
            "reopen",
            {"packet_id": packet["id"], "reason": "Append a new human-reviewed correction"},
            cycle=True,
        )
    )
    assert cycle["state"] == "ACTIVE" and cycle["lock_number"] == 1
    assert export_bytes(client, cycle, 1)[1] == original
    packet = detail(client, packet)
    assert packet["revision"] == 5 and packet["state"] == "DRAFT"
    assert packet["review"] == document["manifest"]["rows"][0]["revision"]["review"]
    historical = ok(client.get(f"{ROOT}/packets/{packet['id']}", params={"revision": 4}))
    assert not historical["can_edit"] and not historical["allowed_actions"]
    approved(client, login, packet)
    login()
    cycle = ok(command(client, cycle_detail(client, cycle), "lock", cycle=True))
    drain(talent_worker, db)
    second, changed = export_bytes(client, cycle)
    assert second["lock_number"] == 2 and changed != original
    assert export_bytes(client, cycle, 1)[1] == original
    assert not export_bytes(client, cycle, 1)[0]["current"]
    assert db.scalar(select(func.count()).select_from(TalentExport)) == 2


@pytest.mark.parametrize("persona", ["finance", "executive", "auditor", "steward"])
def test_raw_routes_forbidden_even_with_known_ids_and_role_headers(client, login, persona):
    cycle = launched(client, login)
    packet = roster(client, cycle)[0]
    login(persona)
    for route in [
        "/cycles",
        f"/cycles/{cycle['id']}",
        f"/cycles/{cycle['id']}/packets",
        f"/packets/{packet['id']}",
        f"/cycles/{cycle['id']}/export",
    ]:
        response = client.get(ROOT + route, headers={"X-Role": "hr", "X-Principal": "hr"})
        assert response.status_code == 403
        assert "accomplishments" not in response.text and "Synthetic Worker" not in response.text
    assert client.get(ROOT + "/options").status_code == 403


def test_peer_packet_context_comments_history_and_export_are_unavailable(client, login):
    cycle = launched(client, login)
    rows = roster(client, cycle)
    peer = next(r for r in rows if r["author_id"] == "manager-b")
    login("manager-a")
    assert len(roster(client, cycle)) == 1
    assert cycle_detail(client, cycle)["population_count"] is None
    for suffix in ["", "?revision=1", "?comment_cursor=" + uid()]:
        assert client.get(f"{ROOT}/packets/{peer['id']}" + suffix).status_code == 404
    assert (
        command(
            client, peer, "comments", {"body": "Try a peer comment", "reason": "Cross scope denial"}
        ).status_code
        == 404
    )
    assert client.get(f"{ROOT}/cycles/{cycle['id']}/export").status_code == 403


def test_calibrator_cannot_author_or_self_approve_and_denial_is_audited_once(client, login, db):
    cycle = launched(client, login)
    packet = roster(client, cycle)[0]
    login("approver")
    packet = detail(client, packet)
    for response in [
        save(client, packet),
        command(
            client, packet, "approve", {"reason": "Attempt self approval", "acknowledgement": True}
        ),
    ]:
        assert response.status_code == 403
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.request_id == response.headers["X-Request-ID"],
                    AuditEvent.permission_result == "DENY",
                )
            )
            == 1
        )
    login(packet["author_id"])
    assert (
        command(
            client,
            detail(client, packet),
            "approve",
            {"reason": "Self approval is forbidden", "acknowledgement": True},
        ).status_code
        == 403
    )


def test_authority_precedes_idempotent_replay_when_write_revoked_but_hr_read_remains(
    client, login, db
):
    cycle = launched(client, login)
    packet = next(r for r in roster(client, cycle) if r["author_id"] == "manager-a")
    login("manager-a")
    packet = detail(client, packet)
    key = uid()
    response = save(client, packet, **{"Idempotency-Key": key})
    assert response.status_code == 200
    assert save(client, packet, **{"Idempotency-Key": key}).json() == response.json()
    grant = db.scalar(
        select(RoleAssignment).where(
            RoleAssignment.principal_id == "manager-a", RoleAssignment.role == "manager"
        )
    )
    grant.effective_to = now() - timedelta(seconds=1)
    db.add(RoleAssignment(principal_id="manager-a", role="hr", scope="*"))
    db.commit()
    assert not detail(client, packet)["can_edit"]
    assert save(client, packet, **{"Idempotency-Key": key}).status_code == 403


def test_versions_idempotency_and_csrf_preserve_unsaved_revision(client, login, db):
    cycle = launched(client, login)
    packet = next(r for r in roster(client, cycle) if r["author_id"] == "manager-a")
    login("manager-a")
    packet = detail(client, packet)
    key = uid()
    saved = ok(save(client, packet, **{"Idempotency-Key": key}))
    assert save(client, packet, "different body", **{"Idempotency-Key": key}).status_code == 409
    assert save(client, packet, "stale client").status_code == 409
    assert save(client, saved, "missing CSRF", **{"X-CSRF-Token": ""}).status_code == 403
    assert detail(client, packet)["revision_id"] == saved["revision_id"]
    assert db.get(ReviewPacket, packet["id"]).version == saved["version"]


@pytest.mark.parametrize("bad", [False, 1, "true", None])
def test_acknowledgement_requires_explicit_boolean(client, login, bad):
    cycle = launched(client, login)
    login("approver")
    packet = detail(client, roster(client, cycle)[0])
    assert (
        command(
            client,
            packet,
            "ack-calibration",
            {"reason": "Explicit human acknowledgement required", "acknowledgement": bad},
        ).status_code
        == 422
    )


def test_foreign_context_and_algorithmic_overposting_are_rejected(client, login):
    cycle = launched(client, login)
    login("manager-a")
    packet = detail(client, roster(client, cycle)[0])
    body = {"review": review(packet), "reason": "Human observation only"}
    body["review"]["evidence"][0]["source_version_id"] = uid()
    assert command(client, packet, "review", body).status_code == 422
    body["review"] = review(packet)
    for field in ["score", "protected_trait", "recommendation"]:
        invalid = copy.deepcopy(body)
        invalid["review"][field] = "Never persisted"
        assert command(client, packet, "review", invalid).status_code == 422
    assert detail(client, packet)["revision"] == 1
    # INITIAL is un-authored; a partial author save exercises checklist validation.
    assert command(client, packet, "submit").status_code == 409
    partial = ok(
        command(client, packet, "review", {"review": {}, "reason": "Persist partial human draft"})
    )
    assert command(client, partial, "submit").status_code == 422


def test_worker_duplicate_delivery_and_crash_after_file_before_receipt(
    client, login, db, talent_worker, monkeypatch
):
    cycle, _ = locked(client, login)
    original = talent_exports.put_private
    stored = []

    def crash(data):
        stored.append(original(data))
        raise RuntimeError("Simulated crash after filesystem commit")

    monkeypatch.setattr(talent_exports, "put_private", crash)
    assert talent_worker() == 0
    assert not db.scalar(select(TalentExport.lock_id))
    assert not db.scalar(
        select(ConsumerReceipt.event_id).where(ConsumerReceipt.consumer == "talent-v1")
    )
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == cycle["id"]))
    assert event.attempts == 1 and event.state == "PENDING"
    event.next_attempt_at = now()
    db.commit()
    monkeypatch.setattr(talent_exports, "put_private", original)
    assert talent_worker() == 1
    artifact = db.scalar(select(TalentExport))
    assert artifact.storage_key == stored[0]
    duplicate = OutboxEvent(
        event_type=event.event_type,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        request_id=uid(),
        payload=event.payload,
    )
    db.add(duplicate)
    db.commit()
    assert talent_worker() == 1
    assert db.scalar(select(func.count()).select_from(TalentExport)) == 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(ConsumerReceipt)
            .where(ConsumerReceipt.consumer == "talent-v1")
        )
        == 2
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.verb == "talent.export_created")
        )
        == 1
    )


def test_worker_five_failures_audited_retry_recovery_no_unlock(
    client, login, db, talent_worker, monkeypatch
):
    cycle, rows = locked(client, login)
    original = talent_exports.put_private

    def unavailable(data):
        raise OSError("Storage outage")

    monkeypatch.setattr(talent_exports, "put_private", unavailable)
    for attempt in range(1, 6):
        assert talent_worker() == 0
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == cycle["id"]))
        assert event.attempts == attempt and event.last_error == "JOB_EXECUTION_FAILED"
        event.next_attempt_at = now()
        db.commit()
    assert event.state == "DEAD"
    assert cycle_detail(client, cycle)["current_export_failed"]
    assert not db.scalar(select(TalentExport.lock_id))
    assert all(db.get(ReviewPacket, r["id"]).state == "LOCKED" for r in rows)
    cycle = ok(command(client, cycle_detail(client, cycle), "retry-export", cycle=True))
    assert cycle["state"] == "LOCKED" and not cycle["current_export_failed"]
    monkeypatch.setattr(talent_exports, "put_private", original)
    drain(talent_worker, db)
    assert export_bytes(client, cycle)[0]["row_count"] == 2


@pytest.mark.parametrize("mismatch", ["source", "aggregate", "version", "kind"])
def test_worker_rejects_wrong_event_binding_without_receipt(
    client, login, db, talent_worker, mismatch
):
    cycle, _ = locked(client, login)
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == cycle["id"]))
    payload = dict(event.payload)
    if mismatch == "source":
        payload["source_batch_id"] = uid()
    elif mismatch == "aggregate":
        payload["entity_id"] = uid()
    elif mismatch == "kind":
        payload["entity_type"] = "comp_cycle"
    else:
        event.aggregate_version += 1
    event.payload = payload
    db.commit()
    assert talent_worker() == 0
    assert not db.scalar(select(TalentExport.lock_id))
    assert not db.scalar(select(ConsumerReceipt.event_id))


def test_export_rechecks_current_session_authority_and_private_bytes(
    client, login, db, talent_worker
):
    cycle, _ = locked(client, login)
    drain(talent_worker, db)
    info, data = export_bytes(client, cycle)
    login()
    assert client.get(info["download_path"]).status_code == 403
    info, _ = export_bytes(client, cycle)
    grant = db.scalar(
        select(RoleAssignment).where(
            RoleAssignment.principal_id == "hr", RoleAssignment.role == "hr"
        )
    )
    grant.effective_to = now() - timedelta(seconds=1)
    db.commit()
    assert client.get(info["download_path"]).status_code == 403
    grant.effective_to = None
    db.commit()
    artifact = db.scalar(select(TalentExport))
    from pathlib import Path

    from meridian.config import settings

    file = Path(settings().local_data_dir) / "objects" / artifact.storage_key
    # Resolve the actual object layout instead of inventing a successful corruption.
    assert file.exists()
    file.write_bytes(data + b"corrupt")
    response = client.get(f"{ROOT}/cycles/{cycle['id']}/export")
    assert response.status_code == 503 and "download_path" not in response.text


def test_auditor_evidence_is_scoped_redacted_and_source_bound(client, login, db, talent_worker):
    cycle, _ = locked(client, login)
    drain(talent_worker, db)
    login("auditor")
    evidence = ok(client.get(f"{ROOT}/evidence/{cycle['id']}"))
    assert evidence["signature_verified"] and evidence["row_count"] == 2
    assert evidence["snapshot_id"] == CURRENT_SNAPSHOT
    rendered = json.dumps(evidence)
    assert all(
        value not in rendered
        for value in [
            "Synthetic narrative",
            "SYN-000025",
            "accomplishments",
            "comments",
            "source_version_id",
            "manager-a",
        ]
    )
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "auditor"))
    grant.scope = "D02"
    db.commit()
    assert client.get(f"{ROOT}/evidence/{cycle['id']}").status_code == 404
    assert not ok(client.get(ROOT + "/evidence"))["items"]


def test_current_manager_mapping_revocation_removes_known_packet(client, login, db):
    cycle = launched(client, login)
    packet = next(r for r in roster(client, cycle) if r["author_id"] == "manager-a")
    login("manager-a")
    db.get(PrincipalEmployee, "manager-a").employee_id = "SYN-000099"
    db.commit()
    assert client.get(f"{ROOT}/packets/{packet['id']}").status_code == 404
    assert client.get(f"{ROOT}/cycles/{cycle['id']}").status_code == 404
    assert not ok(client.get(ROOT + "/cycles"))["items"]


def test_comments_and_decisions_are_bounded_paged_and_peer_ids_do_not_expand_access(
    client, login, talent_worker
):
    cycle = launched(client, login)
    login("manager-a")
    packet = detail(client, roster(client, cycle)[0])
    for index in range(102):
        packet = ok(
            command(
                client,
                packet,
                "comments",
                {
                    "body": f"Synthetic observation number {index}",
                    "reason": "Persist a bounded comment",
                },
            )
        )
    assert len(packet["comments"]) == 25 and packet["comments_next_cursor"]
    assert len(packet["decisions"]) == 100 and packet["decisions_next_version"]
    comment_page = ok(
        client.get(
            f"{ROOT}/packets/{packet['id']}",
            params={"comment_cursor": packet["comments_next_cursor"]},
        )
    )
    assert len(comment_page["comments"]) == 25
    assert not set(c["id"] for c in packet["comments"]).intersection(
        c["id"] for c in comment_page["comments"]
    )
    decision_page = ok(
        client.get(
            f"{ROOT}/packets/{packet['id']}",
            params={"decision_cursor": packet["decisions_next_version"]},
        )
    )
    # Launch adds one immutable decision before the 102 comment commands.
    assert packet["decisions"][0]["action"] == "INITIAL"
    assert len(decision_page["decisions"]) == 3
    assert all(row["action"] == "COMMENT" for row in decision_page["decisions"])
    approved_packet = approved(client, login, packet, calibrate=False)
    assert len(approved_packet["signatures"]) == 2
    assert not any(row["action"] == "APPROVE" for row in approved_packet["decisions"])
    login()
    # Complete the peer packet so the counted cycle can lock.
    peer = next(r for r in roster(client, cycle) if r["id"] != packet["id"])
    approved(client, login, peer, calibrate=False)
    login()
    cycle = ok(command(client, cycle_detail(client, cycle), "lock", cycle=True))
    cycle = ok(
        command(
            client,
            cycle,
            "reopen",
            {"packet_id": packet["id"], "reason": "Append new reviewed correction"},
            cycle=True,
        )
    )
    login("manager-a")
    current = detail(client, packet)
    current = ok(
        command(
            client,
            current,
            "comments",
            {"body": "New post-lock comment", "reason": "Comment on new draft only"},
        )
    )
    assert not current["signatures"]
    historical = ok(
        client.get(
            f"{ROOT}/packets/{packet['id']}", params={"revision": approved_packet["revision"]}
        )
    )
    assert len(historical["signatures"]) == 2
    cursor = None
    old_comments = []
    for _ in range(5):
        page = ok(
            client.get(
                f"{ROOT}/packets/{packet['id']}",
                params={"revision": approved_packet["revision"]}
                | ({"comment_cursor": cursor} if cursor else {}),
            )
        )
        old_comments.extend(row["body"] for row in page["comments"])
        cursor = page["comments_next_cursor"]
        if not cursor:
            break
    assert len(old_comments) == 102 and "New post-lock comment" not in old_comments


@pytest.mark.parametrize(
    "document", [None, [], "bad", {"manifest": []}, {"manifest": {}, "signature": None}]
)
def test_signature_validation_fails_closed_for_malformed_artifact(document):
    assert not talent_exports.signature_valid(document)
