"""Current object/field/action grants combined with frozen compensation assignments."""

from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from meridian.compensation_engine import FrozenEligibility
from meridian.compensation_models import (
    CompApprovalStep,
    CompCycle,
    CompEligibility,
    CompPool,
    CompProposal,
)
from meridian.errors import DomainError
from meridian.identity import Actor
from meridian.identity import scopes as scopes
from meridian.models import Principal, RoleAssignment, now
from meridian.people import visible_query
from meridian.people_models import AuthzPolicy, EmployeeVersion, PrincipalEmployee, Snapshot
from meridian.policy import POLICY_DEFINITION, POLICY_ID, canonical_hash

READ_CAPABILITIES = {
    "comp.manage",
    "comp.read",
    "comp.propose",
    "comp.approve",
    "comp.hr.signoff",
    "comp.finance.signoff",
}


def covers(current: Actor, capability: str, orgs: list[str]) -> bool:
    allowed = scopes(current, capability)
    return "*" in allowed or set(orgs) <= allowed


def principal_grants(
    db: Session, principal_id: str
) -> tuple[Principal, list[RoleAssignment], dict[str, Any]]:
    principal = db.get(Principal, principal_id)
    policy = db.get(AuthzPolicy, POLICY_ID)
    if (
        policy is None
        or policy.sha256 != canonical_hash(policy.definition)
        or policy.sha256 != canonical_hash(POLICY_DEFINITION)
    ):
        raise DomainError(
            503, "policy_unavailable", "The current authorization policy is unavailable."
        )
    if principal is None or not principal.active:
        raise DomainError(
            403, "comp_assignee_unavailable", "A frozen compensation assignee is unavailable."
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
    return principal, grants, policy.definition


def assigned_capability(
    db: Session, principal_id: str, capability: str, org_id: str, chain_ids: set[str] | None = None
) -> None:
    _, grants, policy = principal_grants(db, principal_id)
    allowed = [grant for grant in grants if capability in policy.get(grant.role, [])]
    ordinary = {grant.scope for grant in allowed if grant.role != "manager"}
    manager_link = db.get(PrincipalEmployee, principal_id)
    manager_match = (
        chain_ids is not None
        and manager_link is not None
        and manager_link.employee_id in chain_ids
        and any(grant.role == "manager" for grant in allowed)
    )
    if "*" not in ordinary and org_id not in ordinary and not manager_match:
        raise DomainError(
            403,
            "comp_assignee_scope_denied",
            "A frozen compensation assignment lacks current scoped authority.",
        )


def validate_assignments(
    db: Session, cycle: CompCycle, population: tuple[FrozenEligibility, ...] | None = None
) -> None:
    if population is None:
        rows = list(
            db.scalars(
                select(CompEligibility).where(
                    CompEligibility.cycle_id == cycle.id, CompEligibility.eligible.is_(True)
                )
            )
        )
        items = [
            (
                row.proposer_id,
                row.document["org_id"],
                {link["employee_id"] for link in row.document["chain"][1:]},
            )
            for row in rows
        ]
    else:
        items = [
            (row.proposer_id, row.org_id, {link.employee_id for link in row.chain[1:]})
            for row in population
            if row.eligible
        ]
    for manager, org_id, chain in items:
        if manager is None or org_id is None:
            raise DomainError(
                403, "comp_assignee_unavailable", "The eligible roster lacks a trusted assignment."
            )
        assigned_capability(db, manager, "comp.propose", org_id, chain)
        assigned_capability(db, manager, "people.pay", org_id, chain)
    for assignment in cycle.approvals:
        for field, capability in (
            ("division_approver_id", "comp.approve"),
            ("hr_signoff_id", "comp.hr.signoff"),
            ("finance_signoff_id", "comp.finance.signoff"),
        ):
            assigned_capability(db, assignment[field], capability, assignment["org_id"])
            assigned_capability(db, assignment[field], "people.pay", assignment["org_id"])


def full_cycle_access(current: Actor, cycle: CompCycle) -> bool:
    return any(
        covers(current, capability, cycle.scope_orgs) for capability in ("comp.manage", "comp.read")
    ) and covers(current, "people.pay", cycle.scope_orgs)


def visible_proposals(db: Session, current: Actor, cycle: CompCycle) -> Select[CompProposal]:
    query = select(CompProposal).where(
        CompProposal.cycle_id == cycle.id, CompProposal.state != "BUILDING"
    )
    if "people.pay" not in current.capabilities or not READ_CAPABILITIES.intersection(
        current.capabilities
    ):
        return query.where(CompProposal.id == "NO-SCOPE")
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    if snapshot is None:
        return query.where(CompProposal.id == "NO-SCOPE")
    from meridian.compensation_service import terms_for

    day = terms_for(cycle).eligibility_date
    # Evaluate each field capability separately. Do not let a broad steward/read grant
    # amplify a scoped compensation/pay grant. Bounded IDs avoid duplicate recursive CTEs.
    pay_query = visible_query(db, current, snapshot, day, "people.pay").with_only_columns(
        EmployeeVersion.employee_id
    )
    if full_cycle_access(current, cycle):
        # Keep the current field predicate inside SQL. Materializing a full selected
        # roster per cycle made list/count reads transfer thousands of IDs repeatedly.
        return query.where(CompProposal.employee_id.in_(pay_query))
    pay_ids = set(db.scalars(pay_query.where(EmployeeVersion.employee_id.in_(cycle.selected_ids))))
    permitted: set[str] = set()
    if "comp.propose" in current.capabilities:
        descendants = set(
            db.scalars(
                visible_query(db, current, snapshot, day, "comp.propose")
                .with_only_columns(EmployeeVersion.employee_id)
                .where(EmployeeVersion.employee_id.in_(cycle.selected_ids))
            )
        )
        permitted.update(
            db.scalars(
                select(CompProposal.id).where(
                    CompProposal.cycle_id == cycle.id,
                    CompProposal.proposer_id == current.principal.id,
                    CompProposal.employee_id.in_(pay_ids & descendants),
                )
            )
        )
    required_caps = [
        capability for capability in READ_CAPABILITIES if capability in current.capabilities
    ]
    assigned = select(CompApprovalStep.proposal_id).where(
        CompApprovalStep.assigned_id == current.principal.id,
        CompApprovalStep.capability.in_(required_caps),
    )
    permitted.update(
        db.scalars(
            query.where(
                CompProposal.id.in_(assigned), CompProposal.employee_id.in_(pay_ids)
            ).with_only_columns(CompProposal.id)
        )
    )
    return query.where(CompProposal.id.in_(permitted))


def visible_pool_ids(db: Session, current: Actor, cycle: CompCycle) -> set[str]:
    if full_cycle_access(current, cycle):
        return set(db.scalars(select(CompPool.id).where(CompPool.cycle_id == cycle.id)))
    ids = set(
        db.scalars(
            visible_proposals(db, current, cycle).with_only_columns(CompProposal.employee_id)
        )
    )
    eligible = list(
        db.scalars(
            select(CompEligibility).where(
                CompEligibility.cycle_id == cycle.id, CompEligibility.eligible.is_(True)
            )
        )
    )
    by_pool: dict[str, set[str]] = {}
    for row in eligible:
        if row.pool_id is not None:
            by_pool.setdefault(row.pool_id, set()).add(row.employee_id)
    # A partially authorized pool is withheld entirely; its total could reveal awards
    # outside the caller's frozen roster after a partial scope revocation.
    return {pool for pool, members in by_pool.items() if members <= ids}
