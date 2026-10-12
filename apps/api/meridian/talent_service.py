"""Qualitative review state transitions; no scoring or automated employee outcomes."""

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.governance import record
from meridian.identity import Actor
from meridian.models import uid
from meridian.people_models import AuthzPolicy, EmployeeVersion, Snapshot
from meridian.policy import POLICY_ID, canonical_hash
from meridian.talent_auth import authorization_document, validate_assignments
from meridian.talent_models import (
    CalibrationEvent,
    PacketRevision,
    ReviewPacket,
    TalentComment,
    TalentCycle,
    TalentCycleAction,
    TalentDecision,
    TalentLock,
    TalentPolicy,
)
from meridian.talent_schemas import StructuredReview, TalentCreate
from meridian.talent_seed import TALENT_POLICY


def context_document(source: Snapshot, row: EmployeeVersion) -> dict[str, Any]:
    return {
        "snapshot_id": source.id,
        "source_batch_id": source.source_batch_id,
        "source_version_id": row.id,
        "employee_id": row.employee_id,
        "display_name": row.display_name,
        "org_id": row.org_id,
        "manager_id": row.manager_id,
        "job_family": row.job_family,
        "grade": row.grade,
        "valid_from": row.valid_from.isoformat(),
        "valid_to": row.valid_to.isoformat() if row.valid_to else None,
    }


def policy_for(db: Session, cycle: TalentCycle) -> TalentPolicy:
    policy = db.get(TalentPolicy, cycle.policy_id)
    authz = db.get(AuthzPolicy, cycle.authz_policy_id)
    source = db.get(Snapshot, cycle.snapshot_id)
    if (
        policy is None
        or policy.sha256 != canonical_hash(policy.definition)
        or policy.definition != TALENT_POLICY
        or authz is None
        or authz.sha256 != cycle.authz_sha256
        or authz.sha256 != canonical_hash(authz.definition)
        or source is None
        or source.status != "PUBLISHED"
        or source.sha256 != cycle.snapshot_sha256
    ):
        raise DomainError(
            503, "talent_pinned_input_unavailable", "A pinned source or policy cannot be verified."
        )
    inputs = {
        "snapshot_id": cycle.snapshot_id,
        "snapshot_sha256": cycle.snapshot_sha256,
        "policy_id": policy.id,
        "policy_sha256": policy.sha256,
        "authz_policy_id": cycle.authz_policy_id,
        "authz_sha256": cycle.authz_sha256,
        "as_of": cycle.as_of.isoformat(),
        "due_date": cycle.due_date.isoformat(),
        "assignments": cycle.assignments,
        "scope_orgs": cycle.scope_orgs,
    }
    if canonical_hash(inputs) != cycle.inputs_sha256:
        raise DomainError(
            503, "talent_input_fingerprint_mismatch", "The frozen review inputs cannot be verified."
        )
    return policy


def source_packets(db: Session, cycle: TalentCycle) -> list[ReviewPacket]:
    source = db.get(Snapshot, cycle.snapshot_id)
    if source is None or source.status != "PUBLISHED":
        raise DomainError(422, "talent_source_unavailable", "Select a published synthetic source.")
    active = list(
        db.scalars(
            select(EmployeeVersion)
            .where(
                EmployeeVersion.snapshot_id == cycle.snapshot_id,
                EmployeeVersion.valid_from <= cycle.as_of,
                (EmployeeVersion.valid_to.is_(None) | (EmployeeVersion.valid_to > cycle.as_of)),
            )
            .limit(10001)
        )
    )
    if len(active) > 10000:
        raise DomainError(
            422, "talent_source_work_limit", "Review source exceeds its bounded work budget."
        )
    by_id = {r.employee_id: r for r in active}
    if len(by_id) != len(active):
        raise DomainError(
            503, "talent_source_overlap", "The pinned source has overlapping context."
        )
    result = []
    for assignment in cycle.assignments:
        row = by_id.get(assignment["employee_id"])
        if row is None or row.status != "ACTIVE":
            raise DomainError(
                422,
                "talent_population_invalid",
                "Each review recipient must have an active pinned context.",
            )
        chain = [row.employee_id]
        manager = row.manager_id
        while manager:
            ancestor = by_id.get(manager)
            if ancestor is None or manager in chain or len(chain) >= 64:
                raise DomainError(
                    503,
                    "talent_lineage_unavailable",
                    "The pinned manager lineage cannot be verified.",
                )
            chain.append(manager)
            manager = ancestor.manager_id
        context = context_document(source, row)
        result.append(
            ReviewPacket(
                id=uid(),
                cycle_id=cycle.id,
                employee_id=row.employee_id,
                author_id=assignment["author_id"],
                calibrator_id=assignment["calibrator_id"],
                hr_signer_id=assignment["hr_signer_id"],
                source_version_id=row.id,
                context=context,
                context_sha256=canonical_hash(context),
                chain_ids=chain,
                state="BUILDING",
                version=0,
                current_revision=0,
            )
        )
    validate_assignments(db, result)
    return result


def prepare_cycle(db: Session, current: Actor, body: TalentCreate) -> TalentCycle:
    snapshot = db.get(Snapshot, str(body.snapshot_id))
    policy = db.get(TalentPolicy, str(body.policy_id))
    authz = db.get(AuthzPolicy, POLICY_ID)
    if snapshot is None or snapshot.status != "PUBLISHED" or policy is None or authz is None:
        raise DomainError(422, "talent_configuration_invalid", "Select sealed review inputs.")
    cycle = TalentCycle(
        id=uid(),
        title=body.title,
        owner_id=current.principal.id,
        snapshot_id=snapshot.id,
        snapshot_sha256=snapshot.sha256,
        policy_id=policy.id,
        authz_policy_id=POLICY_ID,
        authz_sha256=authz.sha256,
        as_of=body.as_of,
        due_date=body.due_date,
        assignments=[r.model_dump() for r in sorted(body.assignments, key=lambda a: a.employee_id)],
        scope_orgs=[],
        inputs_sha256="",
        state="CONFIGURING",
        version=1,
        lock_number=0,
    )
    packets = source_packets(db, cycle)
    cycle.scope_orgs = sorted({r.context["org_id"] for r in packets})
    cycle.inputs_sha256 = canonical_hash(
        {
            "snapshot_id": snapshot.id,
            "snapshot_sha256": snapshot.sha256,
            "policy_id": policy.id,
            "policy_sha256": policy.sha256,
            "authz_policy_id": POLICY_ID,
            "authz_sha256": authz.sha256,
            "as_of": cycle.as_of.isoformat(),
            "due_date": cycle.due_date.isoformat(),
            "assignments": cycle.assignments,
            "scope_orgs": cycle.scope_orgs,
        }
    )
    policy_for(db, cycle)
    return cycle


def revision_for(db: Session, packet: ReviewPacket, number: int | None = None) -> PacketRevision:
    row = db.scalar(
        select(PacketRevision).where(
            PacketRevision.packet_id == packet.id,
            PacketRevision.number == (number if number is not None else packet.current_revision),
        )
    )
    if row is None:
        raise DomainError(404, "talent_revision_unavailable", "Review revision is unavailable.")
    return row


def revision_document(
    cycle: TalentCycle, packet: ReviewPacket, row: PacketRevision
) -> dict[str, Any]:
    return {
        "cycle_id": cycle.id,
        "inputs_sha256": cycle.inputs_sha256,
        "packet_id": packet.id,
        "employee_id": packet.employee_id,
        "context_sha256": packet.context_sha256,
        "number": row.number,
        "kind": row.kind,
        "parent_id": row.parent_id,
        "parent_sha256": row.parent_sha256,
        "author_id": row.author_id,
        "reason": row.reason,
        "review": row.review,
    }


def verified_revision(
    db: Session, cycle: TalentCycle, packet: ReviewPacket, row: PacketRevision
) -> StructuredReview:
    policy_for(db, cycle)
    source = db.get(EmployeeVersion, packet.source_version_id)
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    if (
        source is None
        or snapshot is None
        or source.snapshot_id != cycle.snapshot_id
        or source.employee_id != packet.employee_id
        or packet.context_sha256 != canonical_hash(packet.context)
        or packet.context != context_document(snapshot, source)
        or source.status != "ACTIVE"
        or source.valid_from > cycle.as_of
        or source.valid_to is not None
        and source.valid_to <= cycle.as_of
        or row.sha256 != canonical_hash(revision_document(cycle, packet, row))
    ):
        raise DomainError(
            503,
            "talent_revision_fingerprint_mismatch",
            "Pinned review evidence cannot be verified.",
        )
    review = StructuredReview.model_validate(row.review)
    if any(
        str(reference.source_version_id) != packet.source_version_id
        for reference in review.evidence
    ):
        raise DomainError(
            422,
            "talent_context_reference_denied",
            "Evidence must reference this packet's pinned context.",
        )
    return review


def checklist(review: StructuredReview) -> list[str]:
    missing = [
        field
        for field in TALENT_POLICY["required_sections"]
        if len(getattr(review, field).strip()) < 10
    ]
    if not review.evidence:
        missing.append("evidence")
    return missing


def cycle_action(
    db: Session,
    cycle: TalentCycle,
    current: Actor,
    action: str,
    reason: str,
    request_id: str,
    evidence: str,
) -> TalentCycleAction:
    row = TalentCycleAction(
        id=uid(),
        cycle_id=cycle.id,
        actor_id=current.principal.id,
        expected_version=0 if action == "CREATE" else cycle.version,
        action=action,
        reason=reason,
        evidence_sha256=evidence,
        request_id=request_id,
    )
    db.add(row)
    db.flush()
    record(
        db,
        current.principal.id,
        "talent." + action.lower(),
        "talent_cycle",
        cycle.id,
        request_id,
        after_hash=evidence,
        emit=False,
        entity_version=cycle.version,
    )
    return row


def decision(
    db: Session,
    cycle: TalentCycle,
    packet: ReviewPacket,
    current: Actor,
    action: str,
    reason: str,
    request_id: str,
    revision: PacketRevision,
    *,
    state: str | None = None,
    acknowledgement: bool = False,
) -> TalentDecision:
    row = TalentDecision(
        id=uid(),
        packet_id=packet.id,
        revision_id=revision.id,
        actor_id=current.principal.id,
        action=action,
        expected_version=packet.version,
        reason=reason,
        acknowledgement=acknowledgement,
        authorization=authorization_document(db, current),
        request_id=request_id,
    )
    db.add(row)
    db.flush()
    before = (
        revision.parent_sha256
        if action in {"INITIAL", "EDIT", "CALIBRATE", "REOPEN"}
        else revision.sha256
    )
    packet.version += 1
    if state:
        packet.state = state
    packet.current_revision = revision.number
    db.flush()
    record(
        db,
        current.principal.id,
        "talent.packet_" + action.lower(),
        "review_packet",
        packet.id,
        request_id,
        before_hash=before,
        after_hash=revision.sha256,
        emit=False,
        entity_version=packet.version,
    )
    return row


def append_revision(
    db: Session,
    cycle: TalentCycle,
    packet: ReviewPacket,
    current: Actor,
    review: StructuredReview,
    reason: str,
    request_id: str,
    kind: str,
) -> PacketRevision:
    if packet.current_revision >= 101:
        raise DomainError(422, "talent_revision_work_limit", "Review revision work budget reached.")
    previous = revision_for(db, packet) if packet.current_revision else None
    row = PacketRevision(
        id=uid(),
        packet_id=packet.id,
        number=packet.current_revision + 1,
        kind=kind,
        parent_id=previous.id if previous else None,
        parent_sha256=previous.sha256 if previous else None,
        review=review.model_dump(mode="json"),
        author_id=current.principal.id,
        reason=reason,
        sha256="",
    )
    row.sha256 = canonical_hash(revision_document(cycle, packet, row))
    verified_revision(db, cycle, packet, row)
    db.add(row)
    db.flush()
    if kind == "CALIBRATE" and previous:
        if previous.review == row.review:
            raise DomainError(
                422,
                "talent_calibration_unchanged",
                "Calibration edit must change the human narrative.",
            )
        db.add(
            CalibrationEvent(
                revision_id=row.id,
                before_revision_id=previous.id,
                actor_id=current.principal.id,
                reason=reason,
                before_sha256=previous.sha256,
                after_sha256=row.sha256,
            )
        )
        db.flush()
    decision(
        db,
        cycle,
        packet,
        current,
        kind,
        reason,
        request_id,
        row,
        state="CALIBRATING" if kind == "CALIBRATE" else "DRAFT",
    )
    return row


def launch(db: Session, cycle: TalentCycle, current: Actor, reason: str, request_id: str) -> None:
    if cycle.state != "CONFIGURING":
        raise DomainError(409, "talent_launch_unavailable", "Only a configured cycle can launch.")
    policy_for(db, cycle)
    # Construct the cyclic packet/revision pair inside one transaction. The final
    # head FK is still checked at commit (and explicitly in integration tests).
    db.execute(text("SET CONSTRAINTS talent_packet_head DEFERRED"))
    for packet in source_packets(db, cycle):
        db.add(packet)
        db.flush()
        append_revision(
            db, cycle, packet, current, StructuredReview(), reason, request_id, "INITIAL"
        )
    cycle_action(db, cycle, current, "LAUNCH", reason, request_id, cycle.inputs_sha256)
    cycle.state = "ACTIVE"
    cycle.version += 1
    db.flush()


def transition(
    db: Session,
    cycle: TalentCycle,
    packet: ReviewPacket,
    current: Actor,
    action: str,
    reason: str,
    request_id: str,
    *,
    acknowledgement: bool = False,
) -> None:
    if cycle.state != "ACTIVE":
        raise DomainError(409, "talent_cycle_locked", "This review cycle is locked.")
    validate_assignments(db, [packet])
    row = revision_for(db, packet)
    review = verified_revision(db, cycle, packet, row)
    actor = current.principal.id
    if action == "SUBMIT":
        if actor != packet.author_id:
            raise DomainError(403, "talent_author_required", "Only the assigned author can submit.")
        if packet.state not in {"DRAFT", "RETURNED"} or row.kind != "EDIT":
            raise DomainError(
                409,
                "talent_submission_unavailable",
                "Save a new author revision before submission.",
            )
        if checklist(review):
            raise DomainError(
                422,
                "talent_submission_incomplete",
                "Complete all narrative sections and a context reference.",
            )
        state = "SUBMITTED"
    elif action == "START_CALIBRATION":
        if actor != packet.calibrator_id or actor == packet.author_id:
            raise DomainError(
                403,
                "talent_calibrator_required",
                "Only the independent assigned calibrator can act.",
            )
        if packet.state != "SUBMITTED":
            raise DomainError(
                409,
                "talent_calibration_unavailable",
                "Submit the current packet before calibration.",
            )
        state = "CALIBRATING"
    elif action == "ACK_CALIBRATION":
        if actor != packet.calibrator_id or actor == packet.author_id:
            raise DomainError(
                403,
                "talent_calibrator_required",
                "Only the independent assigned calibrator can sign.",
            )
        if packet.state != "CALIBRATING" or not acknowledgement or checklist(review):
            raise DomainError(
                409,
                "talent_calibration_ack_unavailable",
                "Complete calibration and acknowledge this exact revision.",
            )
        state = "CALIBRATED"
    elif action in {"APPROVE", "RETURN"}:
        if actor != packet.hr_signer_id or actor in {packet.author_id, packet.calibrator_id}:
            raise DomainError(
                403,
                "talent_hr_signer_required",
                "Only the independent assigned HR signer can decide.",
            )
        if packet.state != "CALIBRATED":
            raise DomainError(
                409,
                "talent_hr_decision_unavailable",
                "HR decisions require current completed calibration.",
            )
        if action == "APPROVE" and not acknowledgement:
            raise DomainError(
                422,
                "talent_acknowledgement_required",
                "Explicit human review acknowledgement is required.",
            )
        state = "APPROVED" if action == "APPROVE" else "RETURNED"
    else:
        raise DomainError(422, "talent_transition_unknown", "Review action is unsupported.")
    decision(
        db,
        cycle,
        packet,
        current,
        action,
        reason,
        request_id,
        row,
        state=state,
        acknowledgement=acknowledgement,
    )


def save_review(
    db: Session,
    cycle: TalentCycle,
    packet: ReviewPacket,
    current: Actor,
    review: StructuredReview,
    reason: str,
    request_id: str,
    *,
    calibration: bool = False,
) -> None:
    if cycle.state != "ACTIVE":
        raise DomainError(409, "talent_cycle_locked", "This review cycle is locked.")
    validate_assignments(db, [packet])
    if calibration:
        if current.principal.id != packet.calibrator_id:
            raise DomainError(
                403,
                "talent_calibrator_required",
                "Only the assigned independent calibrator can edit.",
            )
        if packet.state != "CALIBRATING":
            raise DomainError(
                409, "talent_calibration_unavailable", "Begin calibration before editing."
            )
    else:
        if current.principal.id != packet.author_id:
            raise DomainError(403, "talent_author_required", "Only the assigned author can edit.")
        if packet.state not in {"DRAFT", "RETURNED"}:
            raise DomainError(
                409, "talent_edit_unavailable", "Submitted or signed packets cannot be edited."
            )
    append_revision(
        db,
        cycle,
        packet,
        current,
        review,
        reason,
        request_id,
        "CALIBRATE" if calibration else "EDIT",
    )


def add_comment(
    db: Session,
    cycle: TalentCycle,
    packet: ReviewPacket,
    current: Actor,
    body: str,
    reason: str,
    request_id: str,
) -> None:
    if cycle.state != "ACTIVE" or packet.state not in {"DRAFT", "RETURNED", "CALIBRATING"}:
        raise DomainError(
            409, "talent_comments_closed", "Comments close before current signatures."
        )
    if current.principal.id not in {packet.author_id, packet.calibrator_id, packet.hr_signer_id}:
        raise DomainError(
            403, "talent_comment_participant_required", "Only assigned participants can comment."
        )
    count = (
        db.scalar(
            select(func.count())
            .select_from(TalentComment)
            .join(ReviewPacket)
            .where(ReviewPacket.cycle_id == cycle.id)
        )
        or 0
    )
    if count >= 2000:
        raise DomainError(422, "talent_comment_work_limit", "Cycle comment work budget reached.")
    revision = revision_for(db, packet)
    item = TalentComment(
        id=uid(),
        packet_id=packet.id,
        revision_id=revision.id,
        author_id=current.principal.id,
        body=body,
        sha256=canonical_hash(
            {
                "packet_id": packet.id,
                "revision_id": revision.id,
                "author_id": current.principal.id,
                "body": body,
            }
        ),
    )
    item.decision_id = uid()
    # Insert the command before its comment, then update the packet after both exist.
    command = TalentDecision(
        id=item.decision_id,
        packet_id=packet.id,
        revision_id=revision.id,
        actor_id=current.principal.id,
        action="COMMENT",
        expected_version=packet.version,
        reason=reason,
        acknowledgement=False,
        authorization=authorization_document(db, current),
        request_id=request_id,
    )
    db.add(command)
    db.flush()
    db.add(item)
    db.flush()
    packet.version += 1
    db.flush()
    record(
        db,
        current.principal.id,
        "talent.comment_created",
        "review_packet",
        packet.id,
        request_id,
        after_hash=item.sha256,
        emit=False,
        entity_version=packet.version,
    )


def lock_cycle(
    db: Session, cycle: TalentCycle, current: Actor, reason: str, request_id: str
) -> TalentLock:
    from meridian.talent_exports import build_export, canonical_bytes

    if cycle.state != "ACTIVE":
        raise DomainError(409, "talent_lock_unavailable", "Only an active approved cycle can lock.")
    packets = list(
        db.scalars(
            select(ReviewPacket)
            .where(ReviewPacket.cycle_id == cycle.id)
            .order_by(ReviewPacket.employee_id)
            .with_for_update()
        )
    )
    validate_assignments(db, packets)
    if len(packets) != len(cycle.assignments) or any(
        r.state not in {"APPROVED", "LOCKED"} for r in packets
    ):
        raise DomainError(
            409,
            "talent_lock_incomplete",
            "Every current packet needs independent calibration and HR approval.",
        )
    refs = []
    for packet in packets:
        revision = revision_for(db, packet)
        verified_revision(db, cycle, packet, revision)
        signatures = list(
            db.scalars(
                select(TalentDecision)
                .where(
                    TalentDecision.packet_id == packet.id,
                    TalentDecision.revision_id == revision.id,
                    TalentDecision.action.in_(["ACK_CALIBRATION", "APPROVE"]),
                )
                .order_by(TalentDecision.expected_version)
            )
        )
        if [r.action for r in signatures] != ["ACK_CALIBRATION", "APPROVE"] or any(
            not r.acknowledgement for r in signatures
        ):
            raise DomainError(
                409, "talent_lock_signature_missing", "Exact current human signatures are missing."
            )
        refs.append(
            {
                "packet_id": packet.id,
                "revision_id": revision.id,
                "sha256": revision.sha256,
                "signature_ids": [r.id for r in signatures],
                "comment_ids": list(
                    db.scalars(
                        select(TalentComment.id)
                        .where(TalentComment.packet_id == packet.id)
                        .order_by(TalentComment.id)
                    )
                ),
            }
        )
    document = {
        "model_version": "human-narrative-context-v1",
        "cycle_id": cycle.id,
        "inputs_sha256": cycle.inputs_sha256,
        "lock_number": cycle.lock_number + 1,
        "row_count": len(refs),
        "packets": refs,
    }
    locked = TalentLock(
        id=uid(),
        cycle_id=cycle.id,
        number=cycle.lock_number + 1,
        document=document,
        sha256=canonical_hash(document),
    )
    if len(canonical_bytes(build_export(db, cycle, locked))) > 8388608:
        raise DomainError(
            422, "talent_export_work_limit", "The exact export exceeds the 8 MiB work budget."
        )
    action = cycle_action(db, cycle, current, "LOCK", reason, request_id, locked.sha256)
    locked.cycle_action_id = action.id
    db.add(locked)
    db.flush()
    for packet in packets:
        if packet.state == "APPROVED":
            decision(
                db,
                cycle,
                packet,
                current,
                "LOCK",
                reason,
                request_id,
                revision_for(db, packet),
                state="LOCKED",
            )
    cycle.state = "LOCKED"
    cycle.lock_number = locked.number
    cycle.version += 1
    db.flush()
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    if snapshot is None:
        raise DomainError(503, "talent_source_unavailable", "The pinned source is unavailable.")
    record(
        db,
        current.principal.id,
        "talent.review_locked",
        "talent_cycle",
        cycle.id,
        request_id,
        after_hash=locked.sha256,
        entity_version=cycle.version,
        event_payload={
            "entity_type": "talent_cycle",
            "entity_id": cycle.id,
            "lock_id": locked.id,
            "source_batch_id": snapshot.source_batch_id,
        },
    )
    return locked


def reopen(
    db: Session,
    cycle: TalentCycle,
    packet: ReviewPacket,
    current: Actor,
    reason: str,
    request_id: str,
) -> None:
    if cycle.state not in {"LOCKED", "ACTIVE"} or packet.state != "LOCKED":
        raise DomainError(
            409, "talent_reopen_unavailable", "Only a locked historical packet can reopen."
        )
    validate_assignments(db, [packet])
    prior = revision_for(db, packet)
    review = verified_revision(db, cycle, packet, prior)
    cycle_action(db, cycle, current, "REOPEN", reason, request_id, prior.sha256)
    cycle.state = "ACTIVE"
    cycle.version += 1
    db.flush()
    append_revision(db, cycle, packet, current, review, reason, request_id, "REOPEN")
