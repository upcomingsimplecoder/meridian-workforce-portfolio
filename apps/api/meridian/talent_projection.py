"""Explicit narrative projection and separately redacted governance evidence."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.identity import Actor
from meridian.models import OutboxEvent
from meridian.talent_auth import assigned_capability, full_access, visible_packets
from meridian.talent_exports import verified_export
from meridian.talent_models import (
    CalibrationEvent,
    PacketRevision,
    ReviewPacket,
    TalentComment,
    TalentCycle,
    TalentDecision,
    TalentExport,
    TalentLock,
)
from meridian.talent_schemas import (
    CalibrationView,
    PacketRevisionView,
    PacketSummary,
    PacketView,
    TalentCommentView,
    TalentContext,
    TalentCycleView,
    TalentDecisionView,
    TalentEvidenceView,
)
from meridian.talent_service import checklist, revision_for, verified_revision


def cycle_view(db: Session, current: Actor, cycle: TalentCycle) -> TalentCycleView:
    query = visible_packets(db, current, cycle)
    states = dict(
        db.execute(
            query.with_only_columns(ReviewPacket.state, func.count()).group_by(ReviewPacket.state)
        ).all()
    )
    locked = db.scalar(
        select(TalentLock).where(
            TalentLock.cycle_id == cycle.id, TalentLock.number == cycle.lock_number
        )
    )
    failed = bool(
        locked
        and db.scalar(
            select(OutboxEvent.id)
            .where(
                OutboxEvent.aggregate_id == cycle.id,
                OutboxEvent.event_type == "talent.review_locked.v1",
                OutboxEvent.payload["lock_id"].astext == locked.id,
                OutboxEvent.state == "DEAD",
            )
            .limit(1)
        )
    )
    return TalentCycleView(
        id=cycle.id,
        title=cycle.title,
        owner_id=cycle.owner_id,
        snapshot_id=cycle.snapshot_id,
        snapshot_sha256=cycle.snapshot_sha256,
        policy_id=cycle.policy_id,
        authz_policy_id=cycle.authz_policy_id,
        inputs_sha256=cycle.inputs_sha256,
        as_of=cycle.as_of,
        due_date=cycle.due_date,
        state=cycle.state,
        version=cycle.version,
        lock_number=cycle.lock_number,
        population_count=len(cycle.assignments) if full_access(current, cycle) else None,
        visible_packets=sum(states.values()),
        states=states,
        can_control=current.principal.id == cycle.owner_id and full_access(current, cycle),
        current_export_ready=bool(locked and db.get(TalentExport, locked.id)),
        current_export_failed=failed,
        created_at=cycle.created_at,
    )


def packet_view(
    db: Session,
    current: Actor,
    cycle: TalentCycle,
    packet: ReviewPacket,
    *,
    number: int | None = None,
    history: bool = True,
    comment_cursor: str | None = None,
    decision_cursor: int | None = None,
) -> PacketView:
    revision = revision_for(db, packet, number)
    review = verified_revision(db, cycle, packet, revision)
    live = number is None or number == packet.current_revision
    active = live and cycle.state == "ACTIVE"
    actor = current.principal.id

    def may_act(capability: str, principal: str) -> bool:
        if actor != principal or capability not in current.capabilities:
            return False
        try:
            assigned_capability(db, actor, capability, packet.context["org_id"], packet.chain_ids)
        except DomainError as error:
            if error.status == 403:
                return False
            raise
        return True

    author = may_act("reviews.write", packet.author_id)
    calibrator = may_act("reviews.approve", packet.calibrator_id)
    hr = may_act("reviews.manage", packet.hr_signer_id)
    editing = active and packet.state in {"DRAFT", "RETURNED"} and author
    calibrating = active and packet.state == "CALIBRATING" and calibrator
    actions = []
    if editing and revision.kind == "EDIT" and not checklist(review):
        actions.append("submit")
    if active and packet.state == "SUBMITTED" and calibrator:
        actions.append("start-calibration")
    if calibrating and not checklist(review):
        actions.append("ack-calibration")
    if active and packet.state == "CALIBRATED" and hr:
        actions.extend(["approve", "return"])
    if (
        active
        and packet.state in {"DRAFT", "RETURNED", "CALIBRATING"}
        and (author or calibrator or hr)
    ):
        actions.append("comment")
    revisions = (
        select(PacketRevision)
        .where(PacketRevision.packet_id == packet.id)
        .order_by(PacketRevision.number)
    )
    decisions = (
        select(TalentDecision)
        .where(TalentDecision.packet_id == packet.id)
        .order_by(TalentDecision.expected_version)
    )
    calibrations = (
        select(CalibrationEvent)
        .join(PacketRevision, CalibrationEvent.revision_id == PacketRevision.id)
        .where(PacketRevision.packet_id == packet.id)
        .order_by(PacketRevision.number)
    )
    if not history:
        revisions = revisions.where(PacketRevision.id == revision.id)
        decisions = decisions.where(TalentDecision.revision_id == revision.id)
        calibrations = calibrations.where(CalibrationEvent.revision_id == revision.id)
    if decision_cursor is not None:
        decisions = decisions.where(TalentDecision.expected_version > decision_cursor)
    decision_rows = list(db.scalars(decisions.limit(101)))
    comments = (
        select(TalentComment)
        .join(PacketRevision, TalentComment.revision_id == PacketRevision.id)
        .where(TalentComment.packet_id == packet.id, PacketRevision.number <= revision.number)
        .order_by(TalentComment.id)
    )
    if not live:
        decisions = decisions.join(
            PacketRevision, TalentDecision.revision_id == PacketRevision.id
        ).where(PacketRevision.number <= revision.number)
        calibrations = calibrations.where(PacketRevision.number <= revision.number)
        decision_rows = list(db.scalars(decisions.limit(101)))
    signatures = list(
        db.scalars(
            select(TalentDecision)
            .where(
                TalentDecision.packet_id == packet.id,
                TalentDecision.revision_id == revision.id,
                TalentDecision.action.in_(["ACK_CALIBRATION", "APPROVE"]),
            )
            .order_by(TalentDecision.expected_version)
            .limit(3)
        )
    )
    if len(signatures) > 2 or any(not row.acknowledgement for row in signatures):
        raise DomainError(
            503, "talent_signature_inconsistent", "Current signatures cannot be verified."
        )
    if comment_cursor:
        comments = comments.where(TalentComment.id > comment_cursor)
    comment_rows = list(db.scalars(comments.limit(26)))
    return PacketView(
        id=packet.id,
        cycle_id=cycle.id,
        employee_id=packet.employee_id,
        display_name=packet.context["display_name"],
        author_id=packet.author_id,
        calibrator_id=packet.calibrator_id,
        hr_signer_id=packet.hr_signer_id,
        source_version_id=packet.source_version_id,
        context=TalentContext.model_validate(packet.context),
        context_sha256=packet.context_sha256,
        state=packet.state,
        version=packet.version,
        current_revision=packet.current_revision,
        revision=revision.number,
        revision_id=revision.id,
        review=review,
        revision_sha256=revision.sha256,
        checklist=checklist(review),
        can_edit=editing,
        can_calibrate=calibrating,
        allowed_actions=actions,
        revisions=[
            PacketRevisionView.model_validate(r, from_attributes=True)
            for r in db.scalars(revisions)
        ],
        decisions=[
            TalentDecisionView.model_validate(r, from_attributes=True) for r in decision_rows[:100]
        ],
        signatures=[TalentDecisionView.model_validate(r, from_attributes=True) for r in signatures],
        decisions_next_version=decision_rows[99].expected_version
        if len(decision_rows) > 100
        else None,
        calibrations=[
            CalibrationView.model_validate(r, from_attributes=True)
            for r in db.scalars(calibrations)
        ],
        comments=[
            TalentCommentView.model_validate(r, from_attributes=True) for r in comment_rows[:25]
        ],
        comments_next_cursor=comment_rows[24].id if len(comment_rows) > 25 else None,
    )


def packet_summary(db: Session, cycle: TalentCycle, packet: ReviewPacket) -> PacketSummary:
    revision = revision_for(db, packet)
    verified_revision(db, cycle, packet, revision)
    return PacketSummary(
        id=packet.id,
        cycle_id=cycle.id,
        employee_id=packet.employee_id,
        display_name=packet.context["display_name"],
        author_id=packet.author_id,
        calibrator_id=packet.calibrator_id,
        hr_signer_id=packet.hr_signer_id,
        state=packet.state,
        version=packet.version,
        current_revision=packet.current_revision,
        revision_sha256=revision.sha256,
    )


def evidence_view(db: Session, cycle: TalentCycle) -> TalentEvidenceView:
    locked = db.scalar(
        select(TalentLock).where(
            TalentLock.cycle_id == cycle.id, TalentLock.number == cycle.lock_number
        )
    )
    artifact = db.get(TalentExport, locked.id) if locked else None
    if locked and artifact:
        verified_export(db, cycle, locked)
    return TalentEvidenceView(
        cycle_id=cycle.id,
        title="Talent review " + cycle.id[:8],
        state=cycle.state,
        snapshot_id=cycle.snapshot_id,
        snapshot_sha256=cycle.snapshot_sha256,
        policy_id=cycle.policy_id,
        authz_policy_id=cycle.authz_policy_id,
        inputs_sha256=cycle.inputs_sha256,
        lock_number=cycle.lock_number,
        lock_sha256=locked.sha256 if locked else None,
        export_sha256=artifact.sha256 if artifact else None,
        row_count=artifact.row_count if artifact else None,
        signature_verified=True if artifact else None,
    )
