"""Persisted Decision Replay and bounded, redacted Audit Explorer."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from meridian.commands import audited_command
from meridian.compensation_models import (
    CompCycle,
    CompProposal,
    CompProposalRevision,
    CompRevisionVerification,
)
from meridian.errors import DomainError
from meridian.idempotency import remember, replay
from meridian.identity import DB, Actor, CurrentActor
from meridian.models import AuditEvent
from meridian.policy import canonical_hash
from meridian.replay_auth import (
    Subject,
    allowed_revisions,
    denial,
    require_global_audit,
    subject_for,
)
from meridian.replay_material import event_document
from meridian.replay_models import ReplayAttempt, ReplayCapsule
from meridian.replay_projection import view
from meridian.replay_schemas import (
    AuditPage,
    AuditQuery,
    ReplayChoice,
    ReplayList,
    ReplayReason,
    ReplayRequest,
    ReplaySummary,
    ReplayView,
)
from meridian.replay_service import actor_reference, append_attempt, create_capsule, verified_packet
from meridian.scenario_models import Scenario, ScenarioRevision, ScenarioVerification

router = APIRouter(prefix="/api/v1/replay", tags=["governance"])
Key = Annotated[UUID, Header(alias="Idempotency-Key")]


def require_evidence(db: Session, current: Actor, request: Request, action: str = "read") -> None:
    if not {"replay.full", "replay.evidence"}.intersection(current.capabilities) or (
        action != "read" and "replay." + action not in current.capabilities
    ):
        denial(db, current, request)


def capsule_for(
    db: Session, current: Actor, request: Request, capsule_id: str, action: str = "read"
) -> tuple[ReplayCapsule, Subject]:
    require_evidence(db, current, request, action)
    capsule = db.get(ReplayCapsule, capsule_id)
    if capsule is None:
        denial(db, current, request, 404)
    subject = subject_for(db, current, request, capsule.kind, capsule.subject_id, action)
    return capsule, subject


@router.get("/choices", response_model=list[ReplayChoice])
def choices(
    current: CurrentActor,
    db: DB,
    request: Request,
    kind: Literal["SCENARIO", "AWARD"] = "SCENARIO",
    search: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> list[ReplayChoice]:
    require_evidence(db, current, request)
    full = "replay.full" in current.capabilities
    if kind == "SCENARIO":
        scenario_query = (
            select(ScenarioRevision, Scenario)
            .join(Scenario, ScenarioRevision.scenario_id == Scenario.id)
            .join(ScenarioVerification, ScenarioVerification.revision_id == ScenarioRevision.id)
            .where(ScenarioRevision.id.in_(allowed_revisions(current, kind)))
        )
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            scenario_query = scenario_query.where(
                (Scenario.title if full else ScenarioRevision.id).ilike(
                    "%" + escaped + "%", escape="\\"
                )
            )
        return [
            ReplayChoice(
                kind="SCENARIO",
                subject_id=revision.id,
                label=f"{scenario.title if full else 'Scenario ' + scenario.id[:8]} · revision {revision.number}",
            )
            for revision, scenario in db.execute(
                scenario_query.order_by(
                    ScenarioRevision.created_at.desc(), ScenarioRevision.id.desc()
                ).limit(50)
            )
        ]
    award_query = (
        select(CompProposalRevision, CompCycle)
        .join(CompProposal, CompProposalRevision.proposal_id == CompProposal.id)
        .join(CompCycle, CompProposal.cycle_id == CompCycle.id)
        .join(
            CompRevisionVerification,
            CompRevisionVerification.revision_id == CompProposalRevision.id,
        )
        .where(CompProposalRevision.id.in_(allowed_revisions(current, kind)))
    )
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        award_query = award_query.where(
            (CompCycle.title if full else CompProposalRevision.id).ilike(
                "%" + escaped + "%", escape="\\"
            )
        )
    return [
        ReplayChoice(
            kind="AWARD",
            subject_id=revision.id,
            label=f"{cycle.title if full else 'Compensation ' + cycle.id[:8]} · award {revision.id[:8]} · revision {revision.number}",
        )
        for revision, cycle in db.execute(
            award_query.order_by(
                CompProposalRevision.created_at.desc(), CompProposalRevision.id.desc()
            ).limit(50)
        )
    ]


@router.get("/capsules", response_model=ReplayList)
def capsules(
    current: CurrentActor,
    db: DB,
    request: Request,
    cursor: UUID | None = None,
    subject_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
) -> ReplayList:
    require_evidence(db, current, request)
    query = select(ReplayCapsule).where(
        or_(
            (ReplayCapsule.kind == "SCENARIO")
            & ReplayCapsule.subject_id.in_(allowed_revisions(current, "SCENARIO")),
            (ReplayCapsule.kind == "AWARD")
            & ReplayCapsule.subject_id.in_(allowed_revisions(current, "AWARD")),
        )
    )
    if subject_id:
        query = query.where(ReplayCapsule.subject_id == str(subject_id))
    if cursor:
        anchor = db.scalar(query.where(ReplayCapsule.id == str(cursor)))
        if anchor is None:
            raise DomainError(422, "replay_cursor_invalid", "Reload the first evidence page.")
        query = query.where(
            tuple_(ReplayCapsule.created_at, ReplayCapsule.id) < (anchor.created_at, anchor.id)
        )
    rows = list(
        db.scalars(
            query.order_by(ReplayCapsule.created_at.desc(), ReplayCapsule.id.desc()).limit(
                limit + 1
            )
        )
    )
    return ReplayList(
        items=[ReplaySummary.model_validate(row, from_attributes=True) for row in rows[:limit]],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.post("/capsules", response_model=ReplayView, status_code=201)
@audited_command
def capture(
    body: ReplayRequest, current: CurrentActor, db: DB, request: Request, key: Key
) -> ReplayView:
    subject = subject_for(db, current, request, body.kind, str(body.subject_id), "verify")
    payload = body.model_dump(mode="json")
    prior = replay(db, current.principal.id, "replay.capture", str(key), payload)
    if prior:
        capsule, subject = capsule_for(db, current, request, prior["id"], "verify")
        return view(db, capsule, subject)
    try:
        capsule = create_capsule(
            db, subject, current.principal.id, body.reason, request.state.request_id
        )
        remember(
            db,
            current.principal.id,
            "replay.capture",
            str(key),
            payload,
            {"id": capsule.id},
            request.state.request_id,
        )
        result = view(db, capsule, subject)
        db.commit()
        return result
    except OSError as error:
        raise DomainError(
            503,
            "replay_storage_unavailable",
            "Evidence could not be persisted. Retry the command with the same key.",
        ) from error


@router.get("/capsules/{capsule_id}", response_model=ReplayView)
def get_capsule(capsule_id: UUID, current: CurrentActor, db: DB, request: Request) -> ReplayView:
    capsule, subject = capsule_for(db, current, request, str(capsule_id))
    return view(db, capsule, subject)


@router.post("/capsules/{capsule_id}/verify", response_model=ReplayView)
@audited_command
def verify(
    capsule_id: UUID,
    body: ReplayReason,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: Key,
    expected: Annotated[int, Header(alias="If-Match", ge=1)],
) -> ReplayView:
    capsule, subject = capsule_for(db, current, request, str(capsule_id), "verify")
    payload = {"capsule_id": capsule.id, "expected_attempt": expected, "reason": body.reason}
    prior = replay(db, current.principal.id, "replay.verify", str(key), payload)
    if prior:
        return view(db, capsule, subject)
    db.scalar(select(ReplayCapsule).where(ReplayCapsule.id == capsule.id).with_for_update())
    latest = db.scalar(
        select(func.max(ReplayAttempt.number)).where(ReplayAttempt.capsule_id == capsule.id)
    )
    if latest != expected:
        raise DomainError(
            409,
            "replay_attempt_conflict",
            "Another verification was recorded. Reload the immutable attempt history.",
        )
    try:
        append_attempt(
            db, capsule, subject, current.principal.id, body.reason, request.state.request_id
        )
        remember(
            db,
            current.principal.id,
            "replay.verify",
            str(key),
            payload,
            {"id": capsule.id},
            request.state.request_id,
        )
        result = view(db, capsule, subject)
        db.commit()
        return result
    except OSError as error:
        raise DomainError(
            503,
            "replay_storage_unavailable",
            "Evidence could not be persisted. Retry the command with the same key.",
        ) from error


@router.get("/capsules/{capsule_id}/attempts/{attempt_id}/export")
def export(
    capsule_id: UUID, attempt_id: UUID, current: CurrentActor, db: DB, request: Request
) -> Response:
    capsule, subject = capsule_for(db, current, request, str(capsule_id), "export")
    attempt = db.get(ReplayAttempt, str(attempt_id))
    if attempt is None or attempt.capsule_id != capsule.id:
        denial(db, current, request, 404)
    raw = verified_packet(db, capsule, attempt, subject)
    return Response(
        raw,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="meridian-replay-{attempt.id}.json"'
        },
    )


@router.get("/audit", response_model=AuditPage)
def audit(
    current: CurrentActor, db: DB, request: Request, query: Annotated[AuditQuery, Query()]
) -> AuditPage:
    require_global_audit(db, current, request)
    statement = select(AuditEvent)
    if query.correlation_id:
        statement = statement.where(AuditEvent.request_id == str(query.correlation_id))
    if query.permission:
        statement = statement.where(AuditEvent.permission_result == query.permission)
    if query.verb:
        statement = statement.where(AuditEvent.verb == query.verb)
    if query.cursor:
        anchor = db.scalar(statement.where(AuditEvent.id == str(query.cursor)))
        if anchor is None:
            raise DomainError(422, "audit_cursor_invalid", "Reload the first audit page.")
        statement = statement.where(
            tuple_(AuditEvent.occurred_at, AuditEvent.id) < (anchor.occurred_at, anchor.id)
        )
    rows = list(
        db.scalars(
            statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(
                query.limit + 1
            )
        )
    )
    return AuditPage(
        items=[
            {
                "id": row.id,
                "verb": row.verb,
                "entity_type": row.entity_type,
                "entity_reference": canonical_hash(row.entity_id)[:16],
                "actor_reference": actor_reference(row.actor_id),
                "request_id": row.request_id,
                "permission": row.permission_result,
                "policy_version": row.policy_version,
                "occurred_at": row.occurred_at,
                "before_sha256": row.before_hash,
                "after_sha256": row.after_hash,
                "sha256": canonical_hash(event_document(row)),
            }
            for row in rows[: query.limit]
        ],
        next_cursor=rows[query.limit - 1].id if len(rows) > query.limit else None,
    )
