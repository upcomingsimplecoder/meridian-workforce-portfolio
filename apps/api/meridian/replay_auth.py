"""Current authority is checked on every source, capsule, receipt and export read."""

from dataclasses import dataclass
from typing import NoReturn

from fastapi import Request
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from meridian.compensation_auth import covers, full_cycle_access, scopes, visible_proposals
from meridian.compensation_models import CompCycle, CompLock, CompProposal, CompProposalRevision
from meridian.errors import DomainError
from meridian.governance import record
from meridian.identity import Actor
from meridian.policy import canonical_hash
from meridian.scenario_models import Scenario, ScenarioRevision


@dataclass(frozen=True)
class Subject:
    kind: str
    revision: ScenarioRevision | CompProposalRevision
    entity: Scenario | CompProposal
    cycle: CompCycle | None
    redacted: bool

    @property
    def snapshot_id(self) -> str:
        return (
            self.entity.base_snapshot_id
            if isinstance(self.entity, Scenario)
            else self.cycle.snapshot_id
            if self.cycle
            else ""
        )


def denial(db: Session, current: Actor, request: Request, status: int = 403) -> NoReturn:
    record(
        db,
        current.principal.id,
        "security.replay_denied",
        "route",
        canonical_hash({"denied_route": request.url.path}),
        request.state.request_id,
        False,
        emit=False,
    )
    db.commit()
    raise DomainError(
        status,
        "replay_unavailable",
        "This evidence is unavailable within your current object and field scope.",
    )


def subject_for(
    db: Session, current: Actor, request: Request, kind: str, revision_id: str, action: str = "read"
) -> Subject:
    full = "replay.full" in current.capabilities
    capability = "replay.full" if full else "replay.evidence"
    if (
        capability not in current.capabilities
        or action != "read"
        and "replay." + action not in current.capabilities
    ):
        denial(db, current, request)
    if kind == "SCENARIO":
        revision = db.get(ScenarioRevision, revision_id)
        scenario = db.get(Scenario, revision.scenario_id) if revision else None
        if (
            revision is None
            or scenario is None
            or not covers(current, capability, scenario.scope_orgs)
        ):
            denial(db, current, request, 404)
        if full and (
            current.principal.id not in {scenario.owner_id, scenario.reviewer_id}
            or not covers(current, "planning.write", scenario.scope_orgs)
            or not covers(current, "people.pay", scenario.scope_orgs)
        ):
            denial(db, current, request, 404)
        return Subject(kind, revision, scenario, None, not full)
    award = db.get(CompProposalRevision, revision_id)
    proposal = db.get(CompProposal, award.proposal_id) if award else None
    cycle = db.get(CompCycle, proposal.cycle_id) if proposal else None
    if (
        award is None
        or proposal is None
        or cycle is None
        or not covers(current, capability, cycle.scope_orgs)
    ):
        denial(db, current, request, 404)
    if full:
        if not full_cycle_access(current, cycle) or not db.scalar(
            visible_proposals(db, current, cycle).where(CompProposal.id == proposal.id)
        ):
            denial(db, current, request, 404)
    else:
        locked = db.scalar(select(CompLock).where(CompLock.cycle_id == cycle.id))
        if locked is None or not any(
            row["revision_id"] == revision_id for row in locked.document["proposal_revisions"]
        ):
            denial(db, current, request, 404)
    return Subject(kind, award, proposal, cycle, not full)


def require_global_audit(db: Session, current: Actor, request: Request) -> None:
    if "*" not in scopes(current, "audit.read"):
        denial(db, current, request)


def allowed_revisions(current: Actor, kind: str) -> Select[str]:
    """The list and cursor use the same current scope as direct object reads."""
    full = "replay.full" in current.capabilities
    capability = "replay.full" if full else "replay.evidence"
    allowed = scopes(current, capability)
    if kind == "SCENARIO":
        query = select(ScenarioRevision.id).join(
            Scenario, ScenarioRevision.scenario_id == Scenario.id
        )
        if "*" not in allowed:
            query = query.where(Scenario.scope_orgs.contained_by(sorted(allowed)))
        if full:
            query = query.where(
                or_(
                    Scenario.owner_id == current.principal.id,
                    Scenario.reviewer_id == current.principal.id,
                )
            )
            for cap in ("planning.write", "people.pay"):
                permitted = scopes(current, cap)
                if "*" not in permitted:
                    query = query.where(Scenario.scope_orgs.contained_by(sorted(permitted)))
        return query
    query = (
        select(CompProposalRevision.id)
        .join(CompProposal, CompProposalRevision.proposal_id == CompProposal.id)
        .join(CompCycle, CompProposal.cycle_id == CompCycle.id)
    )
    if "*" not in allowed:
        query = query.where(CompCycle.scope_orgs.contained_by(sorted(allowed)))
    if full:
        pay = scopes(current, "people.pay")
        if "*" not in pay:
            query = query.where(CompCycle.scope_orgs.contained_by(sorted(pay)))
        management = scopes(current, "comp.manage")
        reading = scopes(current, "comp.read")
        if "*" not in management | reading:
            query = query.where(
                or_(
                    CompCycle.scope_orgs.contained_by(sorted(management)),
                    CompCycle.scope_orgs.contained_by(sorted(reading)),
                )
            )
    else:
        # Locks contain the final revision list; obsolete/returned awards remain withheld.
        query = query.join(CompLock, CompLock.cycle_id == CompCycle.id).where(
            CompLock.document["proposal_revisions"].contains(
                func.jsonb_build_array(
                    func.jsonb_build_object("revision_id", CompProposalRevision.id)
                )
            )
        )
    return query
