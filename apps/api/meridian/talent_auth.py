"""Frozen review assignment intersects current object and narrative field authority."""

from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.identity import Actor
from meridian.models import Principal, RoleAssignment, now
from meridian.people import visible_query
from meridian.people_models import AuthzPolicy, EmployeeVersion, PrincipalEmployee, Snapshot
from meridian.policy import POLICY_DEFINITION, POLICY_ID, canonical_hash
from meridian.talent_models import ReviewPacket, TalentCycle

READ_CAPABILITIES = {"reviews.manage", "reviews.write", "reviews.approve"}


def covers(current: Actor, capability: str, orgs: list[str]) -> bool:
    allowed = {
        row.scope
        for row in current.grants
        if row.role != "manager" and capability in current.policy.get(row.role, [])
    }
    return "*" in allowed or bool(allowed) and set(orgs) <= allowed


def full_access(current: Actor, cycle: TalentCycle) -> bool:
    return covers(current, "reviews.manage", cycle.scope_orgs) and covers(
        current, "people.read", cycle.scope_orgs
    )


def visible_packets(db: Session, current: Actor, cycle: TalentCycle) -> Select[ReviewPacket]:
    query = select(ReviewPacket).where(
        ReviewPacket.cycle_id == cycle.id, ReviewPacket.state != "BUILDING"
    )
    if (
        not READ_CAPABILITIES.intersection(current.capabilities)
        or "people.read" not in current.capabilities
    ):
        return query.where(ReviewPacket.id == "NO-SCOPE")
    source = db.get(Snapshot, cycle.snapshot_id)
    if source is None:
        return query.where(ReviewPacket.id == "NO-SCOPE")
    people = visible_query(db, current, source, cycle.as_of, "people.read").with_only_columns(
        EmployeeVersion.employee_id
    )
    query = query.where(ReviewPacket.employee_id.in_(people))
    if full_access(current, cycle):
        return query
    predicates = []
    for capability, assignment in (
        ("reviews.write", ReviewPacket.author_id),
        ("reviews.approve", ReviewPacket.calibrator_id),
        ("reviews.manage", ReviewPacket.hr_signer_id),
    ):
        if capability in current.capabilities:
            authorized = visible_query(
                db, current, source, cycle.as_of, capability
            ).with_only_columns(EmployeeVersion.employee_id)
            predicates.append(
                (assignment == current.principal.id) & ReviewPacket.employee_id.in_(authorized)
            )
    return query.where(or_(*predicates) if predicates else ReviewPacket.id == "NO-SCOPE")


def assigned_capability(
    db: Session, principal_id: str, capability: str, org_id: str, chain_ids: list[str]
) -> None:
    principal = db.get(Principal, principal_id)
    policy = db.get(AuthzPolicy, POLICY_ID)
    if (
        policy is None
        or policy.sha256 != canonical_hash(policy.definition)
        or policy.sha256 != canonical_hash(POLICY_DEFINITION)
    ):
        raise DomainError(503, "policy_unavailable", "Current review policy is unavailable.")
    if principal is None or not principal.active:
        raise DomainError(
            403, "talent_assignee_unavailable", "A frozen participant is unavailable."
        )
    grants = list(
        db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.principal_id == principal_id,
                RoleAssignment.effective_from <= now(),
                or_(RoleAssignment.effective_to.is_(None), RoleAssignment.effective_to > now()),
            )
        )
    )
    permitted = [g for g in grants if capability in policy.definition.get(g.role, [])]
    if any(g.role != "manager" and g.scope in {"*", org_id} for g in permitted):
        return
    link = db.get(PrincipalEmployee, principal_id)
    if link and link.employee_id in chain_ids[1:] and any(g.role == "manager" for g in permitted):
        return
    raise DomainError(
        403,
        "talent_assignment_scope_denied",
        "A frozen participant lacks current scoped authority.",
    )


def validate_assignments(db: Session, packets: list[ReviewPacket]) -> None:
    for row in packets:
        for principal, capability in (
            (row.author_id, "reviews.write"),
            (row.calibrator_id, "reviews.approve"),
            (row.hr_signer_id, "reviews.manage"),
        ):
            assigned_capability(db, principal, capability, row.context["org_id"], row.chain_ids)
            assigned_capability(db, principal, "people.read", row.context["org_id"], row.chain_ids)


def authorization_document(db: Session, current: Actor) -> dict[str, Any]:
    link = db.get(PrincipalEmployee, current.principal.id)
    return {
        "policy_id": POLICY_ID,
        "policy_sha256": canonical_hash(current.policy),
        "principal_id": current.principal.id,
        "manager_root": link.employee_id if link else None,
        "grants": [
            {
                "id": g.id,
                "role": g.role,
                "scope": g.scope,
                "effective_from": g.effective_from.isoformat(),
                "effective_to": g.effective_to.isoformat() if g.effective_to else None,
            }
            for g in sorted(current.grants, key=lambda g: g.id)
        ],
    }
