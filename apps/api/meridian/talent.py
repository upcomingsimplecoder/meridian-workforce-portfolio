"""Persisted qualitative review API; current scoped grants precede receipt replay."""

from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from meridian.commands import audited_command
from meridian.errors import DomainError
from meridian.governance import record
from meridian.idempotency import remember, replay
from meridian.identity import DB, Actor, CurrentActor, require
from meridian.models import OutboxEvent, Principal, RoleAssignment, now
from meridian.object_store import sign_link, verify_link
from meridian.people import published_snapshots
from meridian.talent_auth import (
    READ_CAPABILITIES,
    assigned_capability,
    covers,
    full_access,
    visible_packets,
)
from meridian.talent_exports import verified_export
from meridian.talent_models import ReviewPacket, TalentCycle, TalentLock, TalentPolicy
from meridian.talent_projection import cycle_view, evidence_view, packet_summary, packet_view
from meridian.talent_schemas import (
    PacketPage,
    PacketView,
    TalentCommentRequest,
    TalentCreate,
    TalentCyclePage,
    TalentCycleView,
    TalentEvidencePage,
    TalentEvidenceView,
    TalentExportView,
    TalentOption,
    TalentOptions,
    TalentQuery,
    TalentReason,
    TalentReopen,
    TalentRevisionQuery,
    TalentSave,
    TalentSignature,
)
from meridian.talent_service import (
    add_comment,
    cycle_action,
    launch,
    lock_cycle,
    prepare_cycle,
    reopen,
    save_review,
    transition,
)

router = APIRouter(prefix="/api/v1/talent", tags=["talent"])
Key = Annotated[UUID, Header(alias="Idempotency-Key")]
Match = Annotated[int, Header(alias="If-Match", ge=1)]


def deny(db: Session, current: Actor, request: Request, status: int = 404) -> NoReturn:
    record(
        db,
        current.principal.id,
        "security.talent_scope_denied",
        "route",
        request.url.path,
        request.state.request_id,
        False,
        emit=False,
    )
    db.commit()
    raise DomainError(
        status,
        "talent_resource_unavailable",
        "Review resource is unavailable in your current scope.",
    )


def require_raw(db: Session, current: Actor, request: Request) -> None:
    if (
        not READ_CAPABILITIES.intersection(current.capabilities)
        or "people.read" not in current.capabilities
    ):
        deny(db, current, request, 403)


def cycle_for(
    db: Session, current: Actor, request: Request, cycle_id: str, *, locked: bool = False
) -> TalentCycle:
    require_raw(db, current, request)
    query = select(TalentCycle).where(TalentCycle.id == cycle_id)
    cycle = db.scalar(query.with_for_update() if locked else query)
    if (
        cycle is None
        or not full_access(current, cycle)
        and db.scalar(visible_packets(db, current, cycle).limit(1)) is None
    ):
        deny(db, current, request)
    return cycle


def packet_for(
    db: Session, current: Actor, request: Request, packet_id: str, *, locked: bool = False
) -> tuple[TalentCycle, ReviewPacket]:
    require_raw(db, current, request)
    candidate = db.get(ReviewPacket, packet_id)
    if candidate is None:
        deny(db, current, request)
    cycle = cycle_for(db, current, request, candidate.cycle_id, locked=locked)
    query = visible_packets(db, current, cycle).where(ReviewPacket.id == packet_id)
    packet = db.scalar(query.with_for_update() if locked else query)
    if packet is None:
        deny(db, current, request)
    return cycle, packet


def controller(db: Session, current: Actor, request: Request, cycle: TalentCycle) -> None:
    require(current, "reviews.manage", db, request)
    if current.principal.id != cycle.owner_id or not full_access(current, cycle):
        deny(db, current, request, 403)


def matching(actual: int, expected: int) -> None:
    if actual != expected:
        raise DomainError(
            409,
            "talent_version_conflict",
            "Review changed. Reload the current revision before acting.",
        )


def participant(
    db: Session,
    current: Actor,
    request: Request,
    packet: ReviewPacket,
    capability: str,
    assigned_id: str,
) -> None:
    require(current, capability, db, request)
    if current.principal.id != assigned_id:
        deny(db, current, request, 403)
    assigned_capability(
        db, current.principal.id, capability, packet.context["org_id"], packet.chain_ids
    )


def payload(body: BaseModel, expected: int | None = None) -> dict[str, Any]:
    return {"body": body.model_dump(mode="json"), "expected_version": expected}


def finish[T: BaseModel](
    db: Session, current: Actor, request: Request, key: UUID, values: dict[str, Any], result: T
) -> T:
    remember(
        db,
        current.principal.id,
        request.url.path,
        str(key),
        values,
        result.model_dump(mode="json"),
        request.state.request_id,
    )
    db.commit()
    return result


@router.get("/options", response_model=TalentOptions)
def options(
    current: CurrentActor,
    db: DB,
    request: Request,
    source_search: Annotated[str, Query(max_length=100)] = "",
) -> TalentOptions:
    require(current, "reviews.manage", db, request)

    def candidates(capability: str) -> list[TalentOption]:
        roles = [name for name, caps in current.policy.items() if capability in caps]
        return [
            TalentOption(id=r.id, label=r.display_name)
            for r in db.scalars(
                select(Principal)
                .join(RoleAssignment)
                .where(
                    Principal.active.is_(True),
                    RoleAssignment.role.in_(roles),
                    RoleAssignment.effective_from <= now(),
                    or_(RoleAssignment.effective_to.is_(None), RoleAssignment.effective_to > now()),
                )
                .distinct()
                .order_by(Principal.id)
                .limit(100)
            )
        ]

    return TalentOptions(
        snapshots=[
            TalentOption(id=r.id, label=r.title) for r in published_snapshots(db, source_search)
        ],
        policies=[
            TalentOption(id=r.id, label=r.title)
            for r in db.scalars(select(TalentPolicy).order_by(TalentPolicy.id).limit(100))
        ],
        authors=candidates("reviews.write"),
        calibrators=candidates("reviews.approve"),
        hr_signers=candidates("reviews.manage"),
    )


@router.get("/cycles", response_model=TalentCyclePage)
def cycles(
    current: CurrentActor, db: DB, request: Request, filters: Annotated[TalentQuery, Query()]
) -> TalentCyclePage:
    require_raw(db, current, request)
    query = select(TalentCycle).order_by(TalentCycle.id)
    if filters.cursor:
        query = query.where(TalentCycle.id > str(filters.cursor))
    if filters.search:
        query = query.where(TalentCycle.title.icontains(filters.search, autoescape=True))
    own = select(ReviewPacket.cycle_id).where(
        or_(
            ReviewPacket.author_id == current.principal.id,
            ReviewPacket.calibrator_id == current.principal.id,
            ReviewPacket.hr_signer_id == current.principal.id,
        )
    )
    scopes = {
        g.scope
        for g in current.grants
        if g.role != "manager" and "reviews.manage" in current.policy.get(g.role, [])
    }
    if "*" not in scopes:
        query = query.where(
            or_(TalentCycle.id.in_(own), TalentCycle.scope_orgs.contained_by(sorted(scopes)))
        )
    candidates = list(db.scalars(query.limit(filters.limit + 1)))
    visible = [
        r
        for r in candidates[: filters.limit]
        if full_access(current, r)
        or db.scalar(visible_packets(db, current, r).limit(1)) is not None
    ]
    return TalentCyclePage(
        items=[cycle_view(db, current, r) for r in visible],
        next_cursor=candidates[filters.limit - 1].id if len(candidates) > filters.limit else None,
    )


@router.post("/cycles", response_model=TalentCycleView)
@audited_command
def create(
    body: TalentCreate, key: Key, current: CurrentActor, db: DB, request: Request
) -> TalentCycleView:
    require(current, "reviews.manage", db, request)
    values = payload(body)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        existing = cycle_for(db, current, request, str(prior["id"]))
        controller(db, current, request, existing)
        return TalentCycleView.model_validate(prior)
    cycle = prepare_cycle(db, current, body)
    if not full_access(current, cycle):
        deny(db, current, request, 403)
    db.add(cycle)
    db.flush()
    cycle_action(
        db, cycle, current, "CREATE", body.reason, request.state.request_id, cycle.inputs_sha256
    )
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.get("/cycles/{cycle_id}", response_model=TalentCycleView)
def detail(cycle_id: UUID, current: CurrentActor, db: DB, request: Request) -> TalentCycleView:
    return cycle_view(db, current, cycle_for(db, current, request, str(cycle_id)))


@router.post("/cycles/{cycle_id}/launch", response_model=TalentCycleView)
@audited_command
def launch_cycle(
    cycle_id: UUID,
    body: TalentReason,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> TalentCycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), locked=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return TalentCycleView.model_validate(prior)
    matching(cycle.version, expected)
    launch(db, cycle, current, body.reason, request.state.request_id)
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.get("/cycles/{cycle_id}/packets", response_model=PacketPage)
def packets(
    cycle_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[TalentQuery, Query()],
) -> PacketPage:
    cycle = cycle_for(db, current, request, str(cycle_id))
    query = visible_packets(db, current, cycle)
    if filters.search:
        query = query.where(ReviewPacket.employee_id.icontains(filters.search, autoescape=True))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if filters.cursor:
        query = query.where(ReviewPacket.id > str(filters.cursor))
    rows = list(db.scalars(query.order_by(ReviewPacket.id).limit(filters.limit + 1)))
    return PacketPage(
        items=[packet_summary(db, cycle, r) for r in rows[: filters.limit]],
        total=total,
        next_cursor=rows[filters.limit - 1].id if len(rows) > filters.limit else None,
    )


@router.get("/packets/{packet_id}", response_model=PacketView)
def packet_detail(
    packet_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[TalentRevisionQuery, Query()],
) -> PacketView:
    cycle, packet = packet_for(db, current, request, str(packet_id))
    return packet_view(
        db,
        current,
        cycle,
        packet,
        number=filters.revision,
        comment_cursor=str(filters.comment_cursor) if filters.comment_cursor else None,
        decision_cursor=filters.decision_cursor,
    )


@router.post("/packets/{packet_id}/review", response_model=PacketView)
@audited_command
def save(
    packet_id: UUID,
    body: TalentSave,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    cycle, packet = packet_for(db, current, request, str(packet_id), locked=True)
    participant(db, current, request, packet, "reviews.write", packet.author_id)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return PacketView.model_validate(prior)
    matching(packet.version, expected)
    save_review(db, cycle, packet, current, body.review, body.reason, request.state.request_id)
    return finish(db, current, request, key, values, packet_view(db, current, cycle, packet))


@router.post("/packets/{packet_id}/calibration", response_model=PacketView)
@audited_command
def calibration(
    packet_id: UUID,
    body: TalentSave,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    cycle, packet = packet_for(db, current, request, str(packet_id), locked=True)
    participant(db, current, request, packet, "reviews.approve", packet.calibrator_id)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return PacketView.model_validate(prior)
    matching(packet.version, expected)
    save_review(
        db,
        cycle,
        packet,
        current,
        body.review,
        body.reason,
        request.state.request_id,
        calibration=True,
    )
    return finish(db, current, request, key, values, packet_view(db, current, cycle, packet))


def act(
    db: Session,
    current: Actor,
    request: Request,
    packet_id: str,
    body: TalentReason,
    key: UUID,
    expected: int,
    action: str,
    acknowledgement: bool = False,
) -> PacketView:
    cycle, packet = packet_for(db, current, request, packet_id, locked=True)
    capability, assigned = (
        ("reviews.write", packet.author_id)
        if action == "SUBMIT"
        else ("reviews.approve", packet.calibrator_id)
        if action in {"START_CALIBRATION", "ACK_CALIBRATION"}
        else ("reviews.manage", packet.hr_signer_id)
    )
    participant(db, current, request, packet, capability, assigned)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return PacketView.model_validate(prior)
    matching(packet.version, expected)
    transition(
        db,
        cycle,
        packet,
        current,
        action,
        body.reason,
        request.state.request_id,
        acknowledgement=acknowledgement,
    )
    return finish(db, current, request, key, values, packet_view(db, current, cycle, packet))


@router.post("/packets/{packet_id}/submit", response_model=PacketView)
@audited_command
def submit(
    packet_id: UUID,
    body: TalentReason,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    return act(db, current, request, str(packet_id), body, key, expected, "SUBMIT")


@router.post("/packets/{packet_id}/start-calibration", response_model=PacketView)
@audited_command
def start_calibration(
    packet_id: UUID,
    body: TalentReason,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    return act(db, current, request, str(packet_id), body, key, expected, "START_CALIBRATION")


@router.post("/packets/{packet_id}/ack-calibration", response_model=PacketView)
@audited_command
def ack_calibration(
    packet_id: UUID,
    body: TalentSignature,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    return act(db, current, request, str(packet_id), body, key, expected, "ACK_CALIBRATION", True)


@router.post("/packets/{packet_id}/approve", response_model=PacketView)
@audited_command
def approve(
    packet_id: UUID,
    body: TalentSignature,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    return act(db, current, request, str(packet_id), body, key, expected, "APPROVE", True)


@router.post("/packets/{packet_id}/return", response_model=PacketView)
@audited_command
def return_packet(
    packet_id: UUID,
    body: TalentReason,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    return act(db, current, request, str(packet_id), body, key, expected, "RETURN")


@router.post("/packets/{packet_id}/comments", response_model=PacketView)
@audited_command
def comment(
    packet_id: UUID,
    body: TalentCommentRequest,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> PacketView:
    cycle, packet = packet_for(db, current, request, str(packet_id), locked=True)
    capability = (
        "reviews.write"
        if current.principal.id == packet.author_id
        else "reviews.approve"
        if current.principal.id == packet.calibrator_id
        else "reviews.manage"
    )
    assigned = (
        packet.author_id
        if capability == "reviews.write"
        else packet.calibrator_id
        if capability == "reviews.approve"
        else packet.hr_signer_id
    )
    participant(db, current, request, packet, capability, assigned)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return PacketView.model_validate(prior)
    matching(packet.version, expected)
    add_comment(db, cycle, packet, current, body.body, body.reason, request.state.request_id)
    return finish(db, current, request, key, values, packet_view(db, current, cycle, packet))


@router.post("/cycles/{cycle_id}/lock", response_model=TalentCycleView)
@audited_command
def lock(
    cycle_id: UUID,
    body: TalentReason,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> TalentCycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), locked=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return TalentCycleView.model_validate(prior)
    matching(cycle.version, expected)
    lock_cycle(db, cycle, current, body.reason, request.state.request_id)
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.post("/cycles/{cycle_id}/reopen", response_model=TalentCycleView)
@audited_command
def reopen_packet(
    cycle_id: UUID,
    body: TalentReopen,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> TalentCycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), locked=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return TalentCycleView.model_validate(prior)
    matching(cycle.version, expected)
    packet = db.scalar(
        visible_packets(db, current, cycle)
        .where(ReviewPacket.id == str(body.packet_id))
        .with_for_update()
    )
    if packet is None:
        deny(db, current, request)
    reopen(db, cycle, packet, current, body.reason, request.state.request_id)
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


def export_access(db: Session, current: Actor, request: Request, cycle: TalentCycle) -> None:
    if not full_access(current, cycle):
        deny(db, current, request, 403)


def lock_for(db: Session, cycle: TalentCycle, number: int | None) -> TalentLock:
    row = db.scalar(
        select(TalentLock).where(
            TalentLock.cycle_id == cycle.id,
            TalentLock.number == (number if number is not None else cycle.lock_number),
        )
    )
    if row is None:
        raise DomainError(
            409, "talent_lock_unavailable", "No immutable lock exists for this version."
        )
    return row


@router.get("/cycles/{cycle_id}/export", response_model=TalentExportView)
def export(
    cycle_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    lock_number: Annotated[int | None, Query(ge=1, le=50000)] = None,
) -> TalentExportView:
    cycle = cycle_for(db, current, request, str(cycle_id))
    export_access(db, current, request, cycle)
    locked = lock_for(db, cycle, lock_number)
    artifact, _ = verified_export(db, cycle, locked)
    token = sign_link(
        current.principal.id, "talent-export:" + current.session.token_hash, cycle.id, locked.number
    )
    record(
        db,
        current.principal.id,
        "talent.export_accessed",
        "talent_cycle",
        cycle.id,
        request.state.request_id,
        after_hash=artifact.sha256,
        emit=False,
        entity_version=locked.number,
    )
    db.commit()
    return TalentExportView(
        cycle_id=cycle.id,
        lock_number=locked.number,
        lock_sha256=locked.sha256,
        export_sha256=artifact.sha256,
        row_count=artifact.row_count,
        current=cycle.state == "LOCKED" and locked.number == cycle.lock_number,
        expires_in_seconds=300,
        download_path=f"/api/v1/talent/cycles/{cycle.id}/download?lock_number={locked.number}&token={token}",
    )


@router.get("/cycles/{cycle_id}/download")
def download(
    cycle_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    token: Annotated[str, Query(min_length=1, max_length=2048)],
    lock_number: Annotated[int, Query(ge=1, le=50000)],
) -> Response:
    cycle = cycle_for(db, current, request, str(cycle_id))
    export_access(db, current, request, cycle)
    verify_link(
        token,
        current.principal.id,
        "talent-export:" + current.session.token_hash,
        cycle.id,
        lock_number,
    )
    artifact, data = verified_export(db, cycle, lock_for(db, cycle, lock_number))
    record(
        db,
        current.principal.id,
        "talent.export_downloaded",
        "talent_cycle",
        cycle.id,
        request.state.request_id,
        after_hash=artifact.sha256,
        emit=False,
        entity_version=lock_number,
    )
    db.commit()
    return Response(
        content=data,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="meridian-talent-{cycle.id}-v{lock_number}.json"'
        },
    )


@router.post("/cycles/{cycle_id}/retry-export", response_model=TalentCycleView)
@audited_command
def retry_export(
    cycle_id: UUID,
    body: TalentReason,
    key: Key,
    expected: Match,
    current: CurrentActor,
    db: DB,
    request: Request,
) -> TalentCycleView:
    cycle = cycle_for(db, current, request, str(cycle_id), locked=True)
    controller(db, current, request, cycle)
    values = payload(body, expected)
    prior = replay(db, current.principal.id, request.url.path, str(key), values)
    if prior is not None:
        return TalentCycleView.model_validate(prior)
    matching(cycle.version, expected)
    locked = lock_for(db, cycle, None)
    event = db.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.aggregate_id == cycle.id,
            OutboxEvent.event_type == "talent.review_locked.v1",
            OutboxEvent.payload["lock_id"].astext == locked.id,
            OutboxEvent.state == "DEAD",
        )
        .with_for_update()
        .limit(1)
    )
    if event is None:
        raise DomainError(
            409, "talent_retry_unavailable", "No failed export job exists for this lock."
        )
    event.state, event.attempts, event.last_error, event.next_attempt_at = "PENDING", 0, None, now()
    record(
        db,
        current.principal.id,
        "talent.export_retry",
        "talent_cycle",
        cycle.id,
        request.state.request_id,
        after_hash=locked.sha256,
        emit=False,
        entity_version=locked.number,
    )
    return finish(db, current, request, key, values, cycle_view(db, current, cycle))


@router.get("/evidence", response_model=TalentEvidencePage)
def evidence_list(
    current: CurrentActor, db: DB, request: Request, filters: Annotated[TalentQuery, Query()]
) -> TalentEvidencePage:
    require(current, "audit.read", db, request)
    query = select(TalentCycle).order_by(TalentCycle.id)
    if filters.cursor:
        query = query.where(TalentCycle.id > str(filters.cursor))
    rows = list(db.scalars(query.limit(filters.limit + 1)))
    items = [
        evidence_view(db, r)
        for r in rows[: filters.limit]
        if covers(current, "audit.read", r.scope_orgs)
    ]
    return TalentEvidencePage(
        items=items, next_cursor=rows[filters.limit - 1].id if len(rows) > filters.limit else None
    )


@router.get("/evidence/{cycle_id}", response_model=TalentEvidenceView)
def evidence(cycle_id: UUID, current: CurrentActor, db: DB, request: Request) -> TalentEvidenceView:
    require(current, "audit.read", db, request)
    cycle = db.get(TalentCycle, str(cycle_id))
    if cycle is None or not covers(current, "audit.read", cycle.scope_orgs):
        deny(db, current, request)
    return evidence_view(db, cycle)
