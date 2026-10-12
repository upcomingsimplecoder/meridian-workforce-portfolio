"""Real PostgreSQL human review graph, immutable reopen and exact private evidence."""

import json
from datetime import date

import pytest
from meridian.errors import DomainError
from meridian.identity import Actor
from meridian.models import Principal, RoleAssignment, uid
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT
from meridian.policy import POLICY_DEFINITION
from meridian.talent_auth import visible_packets
from meridian.talent_exports import build_export, canonical_bytes, verified_export, write_export
from meridian.talent_models import (
    CalibrationEvent,
    ReviewPacket,
    TalentDecision,
    TalentExport,
)
from meridian.talent_schemas import StructuredReview, TalentAssignment, TalentCreate
from meridian.talent_seed import TALENT_POLICY_ID
from meridian.talent_service import (
    add_comment,
    append_revision,
    cycle_action,
    launch,
    lock_cycle,
    prepare_cycle,
    reopen,
    revision_for,
    save_review,
    transition,
)
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError


def authority(db, login, name):
    session = login(name)
    return Actor(
        db.get(Principal, name),
        list(db.scalars(select(RoleAssignment).where(RoleAssignment.principal_id == name))),
        session,
        POLICY_DEFINITION,
    )


def configured(db, login, snapshot=CURRENT_SNAPSHOT):
    hr = authority(db, login, "hr")
    authors = (
        ["manager-a", "manager-b"] if snapshot == CURRENT_SNAPSHOT else ["manager-a", "manager-a"]
    )
    body = TalentCreate(
        title="Synthetic human review",
        snapshot_id=snapshot,
        policy_id=TALENT_POLICY_ID,
        as_of=date(2028, 3, 31),
        due_date=date(2028, 6, 30),
        assignments=[
            TalentAssignment(
                employee_id=employee, author_id=author, calibrator_id="approver", hr_signer_id="hr"
            )
            for employee, author in zip(["SYN-000025", "SYN-000049"], authors, strict=True)
        ],
        reason="Freeze synthetic review context",
    )
    cycle = prepare_cycle(db, hr, body)
    db.add(cycle)
    db.flush()
    cycle_action(db, cycle, hr, "CREATE", body.reason, uid(), cycle.inputs_sha256)
    launch(db, cycle, hr, "Launch frozen human review", uid())
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return cycle, list(
        db.scalars(
            select(ReviewPacket)
            .where(ReviewPacket.cycle_id == cycle.id)
            .order_by(ReviewPacket.employee_id)
        )
    )


def narrative(packet, suffix=""):
    return StructuredReview(
        accomplishments="Synthetic observation: completed a documented demonstration " + suffix,
        collaboration="Synthetic observation: explained the handoff to a peer " + suffix,
        development="Synthetic observation: practice clearer source citations " + suffix,
        evidence=[
            {
                "source_version_id": packet.source_version_id,
                "observation": "Synthetic observation anchored to this exact job context",
            }
        ],
    )


def approved(db, login, cycle, packet, *, calibrate=True):
    author = authority(db, login, packet.author_id)
    cal = authority(db, login, "approver")
    hr = authority(db, login, "hr")
    save_review(db, cycle, packet, author, narrative(packet), "Record human observations", uid())
    transition(db, cycle, packet, author, "SUBMIT", "Submit this exact human review", uid())
    transition(db, cycle, packet, cal, "START_CALIBRATION", "Begin independent calibration", uid())
    if calibrate:
        save_review(
            db,
            cycle,
            packet,
            cal,
            narrative(packet, "after calibration"),
            "Clarify synthetic observable evidence",
            uid(),
            calibration=True,
        )
    transition(
        db,
        cycle,
        packet,
        cal,
        "ACK_CALIBRATION",
        "Participated in calibration and reviewed this revision",
        uid(),
        acknowledgement=True,
    )
    transition(
        db,
        cycle,
        packet,
        hr,
        "APPROVE",
        "Human HR review of the exact calibrated revision",
        uid(),
        acknowledgement=True,
    )


def test_calibration_lock_reopen_keeps_prior_export_exact(db, login):
    cycle, packets = configured(db, login)
    author = authority(db, login, "manager-a")
    add_comment(
        db,
        cycle,
        packets[0],
        author,
        "Synthetic clarification before independent review",
        "Record scoped comment",
        uid(),
    )
    for packet in packets:
        approved(db, login, cycle, packet)
    hr = authority(db, login, "hr")
    first = lock_cycle(db, cycle, hr, "Lock independently signed review packets", uid())
    source = packets[0].context["source_batch_id"]
    one = write_export(db, cycle.id, first.id, source)
    old_data = verified_export(db, cycle, first)[1]
    doc = json.loads(old_data)
    assert one.row_count == 2 and cycle.state == "LOCKED" and cycle.lock_number == 1
    assert doc["manifest"]["rows"][0]["revision"]["number"] == 3
    assert len(doc["manifest"]["rows"][0]["calibrations"]) == 1
    assert [s["action"] for s in doc["manifest"]["rows"][0]["signatures"]] == [
        "ACK_CALIBRATION",
        "APPROVE",
    ]
    assert "annual_pay" not in old_data.decode() and "score" not in old_data.decode()
    assert write_export(db, cycle.id, first.id, source).storage_key == one.storage_key
    event = db.get(CalibrationEvent, revision_for(db, packets[0]).id)
    assert event.before_sha256 != event.after_sha256
    reopen(db, cycle, packets[0], hr, "Reopen as a new immutable author revision", uid())
    assert (
        packets[0].current_revision == 4
        and packets[0].state == "DRAFT"
        and packets[1].state == "LOCKED"
    )
    assert revision_for(db, packets[0]).kind == "REOPEN"
    add_comment(
        db,
        cycle,
        packets[0],
        author,
        "New synthetic comment after reopening",
        "Reopened context clarification",
        uid(),
    )
    approved(db, login, cycle, packets[0], calibrate=False)
    second = lock_cycle(db, cycle, hr, "Lock replacement exact signatures", uid())
    two = write_export(db, cycle.id, second.id, source)
    assert two.row_count == 2 and two.storage_key != one.storage_key
    assert cycle.lock_number == 2 and packets[0].current_revision == 5
    assert canonical_bytes(build_export(db, cycle, first)) == old_data
    assert verified_export(db, cycle, first)[1] == old_data
    assert write_export(db, cycle.id, first.id, source).storage_key == one.storage_key
    assert db.scalar(select(func.count()).select_from(TalentExport)) == 2


def test_return_requires_new_author_edit_and_fresh_calibration(db, login):
    cycle, packets = configured(db, login)
    packet = packets[0]
    author = authority(db, login, packet.author_id)
    cal = authority(db, login, "approver")
    hr = authority(db, login, "hr")
    save_review(db, cycle, packet, author, narrative(packet), "Record synthetic narrative", uid())
    transition(db, cycle, packet, author, "SUBMIT", "Submit synthetic narrative", uid())
    transition(db, cycle, packet, cal, "START_CALIBRATION", "Start human calibration", uid())
    transition(
        db,
        cycle,
        packet,
        cal,
        "ACK_CALIBRATION",
        "Acknowledge current calibrated review",
        uid(),
        acknowledgement=True,
    )
    transition(db, cycle, packet, hr, "RETURN", "Request a clearer observable reference", uid())
    with pytest.raises(DomainError) as error:
        transition(
            db,
            cycle,
            packet,
            hr,
            "APPROVE",
            "Attempt a stale signature",
            uid(),
            acknowledgement=True,
        )
    assert error.value.status == 409
    approved(db, login, cycle, packet, calibrate=False)
    current = revision_for(db, packet)
    assert current.number == 3 and packet.state == "APPROVED"
    assert (
        db.scalar(
            select(func.count())
            .select_from(TalentDecision)
            .where(
                TalentDecision.packet_id == packet.id,
                TalentDecision.revision_id == current.id,
                TalentDecision.action == "APPROVE",
            )
        )
        == 1
    )


@pytest.mark.parametrize("action", ["START_CALIBRATION", "ACK_CALIBRATION", "APPROVE", "RETURN"])
def test_author_never_self_calibrates_or_approves(db, login, action):
    cycle, packets = configured(db, login)
    with pytest.raises(DomainError) as error:
        transition(
            db,
            cycle,
            packets[0],
            authority(db, login, "manager-a"),
            action,
            "Forbidden self decision",
            uid(),
            acknowledgement=True,
        )
    assert error.value.status == 403 and packets[0].state == "DRAFT"


def test_foreign_context_and_incomplete_submission_block(db, login):
    cycle, packets = configured(db, login)
    packet = packets[0]
    author = authority(db, login, packet.author_id)
    bad = narrative(packets[1])
    with pytest.raises(DomainError) as error, db.begin_nested():
        save_review(db, cycle, packet, author, bad, "Attempt peer context citation", uid())
    assert error.value.status == 422 and packet.current_revision == 1
    save_review(
        db,
        cycle,
        packet,
        author,
        StructuredReview(accomplishments="Partially authored synthetic draft"),
        "Save partial narrative draft",
        uid(),
    )
    with pytest.raises(DomainError) as error:
        transition(db, cycle, packet, author, "SUBMIT", "Attempt incomplete review", uid())
    assert error.value.status == 422 and packet.state == "DRAFT"
    assert packet.current_revision == 2


def test_locked_actions_block_and_frozen_scope_survives_source_correction(db, login):
    old, old_packets = configured(db, login, BASE_SNAPSHOT)
    new, new_packets = configured(db, login)
    manager_a = authority(db, login, "manager-a")
    manager_b = authority(db, login, "manager-b")
    assert [
        r.employee_id
        for r in db.scalars(visible_packets(db, manager_a, old).order_by(ReviewPacket.employee_id))
    ] == ["SYN-000025", "SYN-000049"]
    assert [r.employee_id for r in db.scalars(visible_packets(db, manager_a, new))] == [
        "SYN-000025"
    ]
    assert list(db.scalars(visible_packets(db, manager_b, old))) == []
    assert [r.employee_id for r in db.scalars(visible_packets(db, manager_b, new))] == [
        "SYN-000049"
    ]
    for packet in old_packets:
        approved(db, login, old, packet, calibrate=False)
    lock_cycle(
        db, old, authority(db, login, "hr"), "Lock old source without retroactive mutation", uid()
    )
    with pytest.raises(DomainError) as error:
        save_review(
            db,
            old,
            old_packets[0],
            manager_a,
            narrative(old_packets[0]),
            "Forbidden locked narrative",
            uid(),
        )
    assert error.value.status == 409
    assert old_packets[1].source_version_id != new_packets[1].source_version_id


@pytest.mark.parametrize(
    "table,sql",
    [
        ("talent_cycle", "SET title='forged'"),
        ("review_packet", "SET author_id='manager-b'"),
        ("review_packet", "SET state='APPROVED',version=version+1"),
        ("packet_revision", "SET reason='forged history'"),
        ("talent_cycle_action", "SET reason='forged action'"),
        ("talent_decision", "SET acknowledgement=true"),
    ],
)
def test_sql_cannot_rewrite_or_skip_review_history(db, login, table, sql):
    configured(db, login)
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text(f"UPDATE {table} {sql}"))


def test_calibration_cannot_be_an_unchanged_edit(db, login):
    cycle, packets = configured(db, login)
    packet = packets[0]
    author = authority(db, login, packet.author_id)
    cal = authority(db, login, "approver")
    save_review(db, cycle, packet, author, narrative(packet), "Human observation draft", uid())
    transition(db, cycle, packet, author, "SUBMIT", "Submit current observations", uid())
    transition(db, cycle, packet, cal, "START_CALIBRATION", "Begin calibration session", uid())
    with pytest.raises((DomainError, DBAPIError)), db.begin_nested():
        append_revision(
            db,
            cycle,
            packet,
            cal,
            narrative(packet),
            "Unchanged calibration must not masquerade as an edit",
            uid(),
            "CALIBRATE",
        )
    assert packet.current_revision == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"score": 5},
        {"protected_trait": "inferred"},
        {"recommendation": "compensation"},
    ],
)
def test_review_schema_has_no_scoring_or_personnel_recommendation_fields(mutation):
    with pytest.raises(ValidationError):
        StructuredReview.model_validate(mutation)


def test_forged_source_event_cannot_write_a_review_artifact(db, login):
    cycle, packets = configured(db, login)
    for packet in packets:
        approved(db, login, cycle, packet, calibrate=False)
    locked = lock_cycle(db, cycle, authority(db, login, "hr"), "Lock exact human signatures", uid())
    with pytest.raises(DomainError) as error:
        write_export(db, cycle.id, locked.id, uid())
    assert error.value.status == 503
    assert db.get(TalentExport, locked.id) is None


def test_scope_revocation_removes_existing_author_access(db, login):
    from meridian.people_models import PrincipalEmployee

    cycle, _ = configured(db, login)
    actor = authority(db, login, "manager-a")
    assert len(list(db.scalars(visible_packets(db, actor, cycle)))) == 1
    db.get(PrincipalEmployee, "manager-a").employee_id = "SYN-000003"
    db.flush()
    assert list(db.scalars(visible_packets(db, actor, cycle))) == []
