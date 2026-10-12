"""Real PostgreSQL compensation workflow and immutable financial/human evidence."""

import pytest
from meridian.compensation_approvals import (
    approvals_for,
    decide,
    lock_cycle,
    start_review,
    submit_proposal,
)
from meridian.compensation_models import (
    CompApprovalStep,
    CompDecision,
    CompEligibility,
    CompLock,
    CompProposal,
    CompReviewRound,
    CompRevisionVerification,
)
from meridian.compensation_schemas import CycleCreate
from meridian.compensation_seed import GUIDELINE_ID
from meridian.compensation_service import (
    check_inputs,
    launch_cycle,
    pool_totals,
    prepare_cycle,
    revision_for,
    save_revision,
    verify_revision,
)
from meridian.finance_seed import FX_BASE_ID
from meridian.models import uid
from meridian.people_models import AuthzPolicy, Snapshot
from meridian.people_seed import CURRENT_SNAPSHOT
from meridian.policy import POLICY_DEFINITION, POLICY_ID, POLICY_V1_DEFINITION, canonical_hash
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


def configuration(**override):
    return {
        "title": "Synthetic 2028 compensation",
        "snapshot_id": CURRENT_SNAPSHOT,
        "guideline_id": GUIDELINE_ID,
        "fx_set_id": FX_BASE_ID,
        "selected_ids": ["SYN-000025", "SYN-000049", "SYN-000073"],
        "terms": {
            "eligibility_date": "2028-03-31",
            "merit_effective_date": "2028-05-01",
            "bonus_service_start": "2028-01-01",
            "bonus_service_end": "2029-01-01",
            "bonus_payment_date": "2029-01-15",
        },
        "pools": [
            {
                "manager_id": "manager-a",
                "org_id": "D01",
                "currency": "USD",
                "budget_minor": "2000000",
                "holdback_minor": "0",
            },
            {
                "manager_id": "manager-b",
                "org_id": "D01",
                "currency": "USD",
                "budget_minor": "500000",
                "holdback_minor": "100000",
            },
        ],
        "approvals": [
            {
                "org_id": "D01",
                "division_approver_id": "approver",
                "hr_signoff_id": "hr",
                "finance_signoff_id": "finance",
            }
        ],
        "reason": "Synthetic cycle configuration",
    } | override


def configured(db, **override):
    cycle, pools, population = prepare_cycle(
        db, CycleCreate.model_validate(configuration(**override)), "hr", POLICY_ID
    )
    db.add(cycle)
    db.flush()
    db.add_all(pools)
    db.flush()
    assert len(population) == 3
    return cycle


def launched(db, **override):
    cycle = configured(db, **override)
    launch_cycle(db, cycle, reason="Launch synthetic compensation cycle", request_id=uid())
    return cycle


def worker_verify(db, cycle):
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    for proposal in db.scalars(select(CompProposal).where(CompProposal.cycle_id == cycle.id)):
        revision = revision_for(db, proposal)
        verify_revision(db, proposal.id, revision.id, cycle.id, snapshot.source_batch_id)


def submitted(db, *, exception=True):
    cycle = launched(db)
    for proposal in db.scalars(select(CompProposal).where(CompProposal.cycle_id == cycle.id)):
        save_revision(
            db,
            cycle,
            proposal,
            merit_minor="1000000"
            if exception and proposal.proposer_id == "manager-a"
            else "120000",
            bonus_minor="100000",
            comment="Synthetic award rationale",
            reason="Manager proposal",
            author_id=proposal.proposer_id,
            kind="EDIT",
            request_id=uid(),
        )
    worker_verify(db, cycle)
    for proposal in db.scalars(select(CompProposal).where(CompProposal.cycle_id == cycle.id)):
        submit_proposal(
            db, cycle, proposal, reason="Submit proposed award for review", request_id=uid()
        )
    return cycle


def division_and_hr(db, cycle, *, return_employee=None):
    for proposal in db.scalars(
        select(CompProposal)
        .where(CompProposal.cycle_id == cycle.id)
        .order_by(CompProposal.employee_id)
    ):
        start_review(
            db,
            cycle,
            proposal,
            actor_id="approver",
            reason="Begin independent division review",
            request_id=uid(),
        )
        revision = revision_for(db, proposal)
        decide(
            db,
            cycle,
            proposal,
            actor_id="approver",
            action="APPROVE",
            reason="Division verifies proposed award and guideline exception",
            exceptions=revision.award["exceptions"],
            request_id=uid(),
        )
        if proposal.employee_id == return_employee:
            decide(
                db,
                cycle,
                proposal,
                actor_id="hr",
                action="RETURN",
                reason="Revise award rationale and allocation",
                exceptions=[],
                request_id=uid(),
            )
        else:
            decide(
                db,
                cycle,
                proposal,
                actor_id="hr",
                action="APPROVE",
                reason="HR verifies eligibility and governance",
                exceptions=[],
                request_id=uid(),
            )


def finance_approve(db, cycle, proposal):
    from meridian.compensation_approvals import next_step, required_exceptions

    step = next_step(db, cycle, proposal)
    decide(
        db,
        cycle,
        proposal,
        actor_id="finance",
        action="APPROVE",
        reason="Finance signs the pinned financial bundle and holdback release",
        exceptions=required_exceptions(db, cycle, proposal, step),
        request_id=uid(),
    )


def test_full_serial_exception_return_revise_and_atomic_lock_golden(db):
    cycle = submitted(db)
    division_and_hr(db, cycle, return_employee="SYN-000049")
    assert cycle.state == "REVIEWING" and cycle.current_round_id is None
    returned = db.scalar(
        select(CompProposal).where(
            CompProposal.cycle_id == cycle.id, CompProposal.employee_id == "SYN-000049"
        )
    )
    assert returned.state == "RETURNED" and returned.current_revision == 3
    old_division = list(
        db.scalars(
            select(CompDecision).where(
                CompDecision.proposal_id == returned.id, CompDecision.action == "APPROVE"
            )
        )
    )
    assert len(old_division) == 1
    assert approvals_for(db, cycle, returned, revision_for(db, returned).id) == {}
    save_revision(
        db,
        cycle,
        returned,
        merit_minor="100000",
        bonus_minor="100000",
        comment="Revised synthetic rationale and smaller increment",
        reason="Respond to HR return",
        author_id="manager-b",
        kind="EDIT",
        request_id=uid(),
    )
    worker_verify(db, cycle)
    submit_proposal(db, cycle, returned, reason="Resubmit corrected proposal", request_id=uid())
    start_review(
        db, cycle, returned, actor_id="approver", reason="Review the new revision", request_id=uid()
    )
    for actor in ("approver", "hr"):
        decide(
            db,
            cycle,
            returned,
            actor_id=actor,
            action="APPROVE",
            reason="Approve corrected revision independently",
            exceptions=[],
            request_id=uid(),
        )
    assert cycle.state == "FINAL_APPROVAL" and cycle.current_round_id is not None
    review_round = db.get(CompReviewRound, cycle.current_round_id)
    assert review_round.document["totals_by_currency"] == {
        "USD": {
            "annualized_merit_minor": "1220000",
            "merit_first_year_minor": "813328",
            "bonus_payable_minor": "300000",
            "pool_charge_minor": "1520000",
        }
    }
    assert review_round.document["reporting_pool_charge_minor"] == "1520000"
    assert (
        next(pool for pool in review_round.document["pools"] if pool["manager_id"] == "manager-b")[
            "holdback_required_minor"
        ]
        == "20000"
    )
    for proposal in db.scalars(select(CompProposal).where(CompProposal.cycle_id == cycle.id)):
        finance_approve(db, cycle, proposal)
    locked = lock_cycle(
        db, cycle, reason="HR finalizes fully signed synthetic cycle", request_id=uid()
    )
    assert cycle.state == "LOCKED"
    assert db.scalar(select(func.count()).select_from(CompLock)) == 1
    assert locked.document["row_count"] == 3 and locked.sha256 == canonical_hash(locked.document)
    assert all(
        proposal.state == "LOCKED"
        for proposal in db.scalars(select(CompProposal).where(CompProposal.cycle_id == cycle.id))
    )
    assert db.get(CompDecision, old_division[0].id).revision_id != revision_for(db, returned).id


def test_finance_return_invalidates_sibling_signatures_and_requires_new_round(db):
    cycle = submitted(db, exception=False)
    division_and_hr(db, cycle)
    old_round = cycle.current_round_id
    proposals = list(
        db.scalars(
            select(CompProposal)
            .where(CompProposal.cycle_id == cycle.id)
            .order_by(CompProposal.employee_id)
        )
    )
    for proposal in proposals[:2]:
        finance_approve(db, cycle, proposal)
    decide(
        db,
        cycle,
        proposals[2],
        actor_id="finance",
        action="RETURN",
        reason="Finance requests a revised allocation before finalization",
        exceptions=[],
        request_id=uid(),
    )
    assert cycle.state == "REVIEWING" and cycle.current_round_id is None
    assert [row.state for row in proposals] == ["APPROVAL_PENDING", "APPROVAL_PENDING", "RETURNED"]
    assert all(
        3 not in approvals_for(db, cycle, proposal, revision_for(db, proposal).id)
        for proposal in proposals
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(CompDecision)
            .where(CompDecision.action == "INVALIDATE_FINANCE")
        )
        == 2
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(CompDecision)
            .where(CompDecision.round_id == old_round, CompDecision.action == "APPROVE")
        )
        == 2
    )
    returned = proposals[2]
    save_revision(
        db,
        cycle,
        returned,
        merit_minor="100000",
        bonus_minor="100000",
        comment="Respond to Finance allocation return",
        reason="Manager revises financial allocation",
        author_id="manager-b",
        kind="EDIT",
        request_id=uid(),
    )
    worker_verify(db, cycle)
    submit_proposal(db, cycle, returned, reason="Resubmit to the complete chain", request_id=uid())
    start_review(
        db, cycle, returned, actor_id="approver", reason="Recheck new revision", request_id=uid()
    )
    for actor in ("approver", "hr"):
        decide(
            db,
            cycle,
            returned,
            actor_id=actor,
            action="APPROVE",
            reason="Review the new revision independently",
            exceptions=[],
            request_id=uid(),
        )
    assert cycle.state == "FINAL_APPROVAL" and cycle.current_round_id != old_round
    assert db.scalar(select(func.count()).select_from(CompReviewRound)) == 2
    assert all(proposal.state == "APPROVAL_PENDING" for proposal in proposals)
    for proposal in proposals:
        finance_approve(db, cycle, proposal)
    lock_cycle(db, cycle, reason="Finalize replacement Finance round", request_id=uid())
    assert cycle.state == "LOCKED"


def test_explicit_v2_policy_preserves_v1_and_separates_finance_signoff(db):
    assert db.get(AuthzPolicy, "authz-v1").definition == POLICY_V1_DEFINITION
    assert db.get(AuthzPolicy, POLICY_ID).definition == POLICY_DEFINITION
    assert db.get(AuthzPolicy, "authz-v1").sha256 == canonical_hash(POLICY_V1_DEFINITION)
    assert "comp.finance.signoff" not in POLICY_V1_DEFINITION["finance"]
    assert "comp.finance.signoff" in POLICY_DEFINITION["finance"]
    assert "comp.finance.signoff" not in POLICY_DEFINITION["manager"]


def test_launch_freezes_three_assigned_rosters_zero_awards_and_nine_serial_steps(db):
    cycle = launched(db)
    assert cycle.state == "OPEN" and cycle.version == 2
    assert check_inputs(db, cycle)["selected_ids"] == ["SYN-000025", "SYN-000049", "SYN-000073"]
    proposals = list(
        db.scalars(
            select(CompProposal)
            .where(CompProposal.cycle_id == cycle.id)
            .order_by(CompProposal.employee_id)
        )
    )
    assert [row.proposer_id for row in proposals] == ["manager-a", "manager-b", "manager-b"]
    assert all(
        row.state == "DRAFT" and row.current_revision == 1 and row.version == 1 for row in proposals
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(CompEligibility)
            .where(CompEligibility.cycle_id == cycle.id)
        )
        == 3
    )
    assert db.scalar(select(func.count()).select_from(CompApprovalStep)) == 9
    for proposal in proposals:
        revision = revision_for(db, proposal)
        assert revision.award["pool_charge_minor"] == "0"
        assert revision.award["warnings"] == ["ZERO_AWARD"]
        assert [
            step.assigned_id
            for step in db.scalars(
                select(CompApprovalStep)
                .where(CompApprovalStep.revision_id == revision.id)
                .order_by(CompApprovalStep.ordinal)
            )
        ] == ["approver", "hr", "finance"]
    worker_verify(db, cycle)
    worker_verify(db, cycle)
    assert db.scalar(select(func.count()).select_from(CompRevisionVerification)) == 3


def test_saved_award_is_immutable_and_exact_pool_holdback_exception_is_visible(db):
    cycle = launched(db)
    for proposal in db.scalars(select(CompProposal).where(CompProposal.cycle_id == cycle.id)):
        save_revision(
            db,
            cycle,
            proposal,
            merit_minor="120000",
            bonus_minor="100000",
            comment="Synthetic award rationale",
            reason="Manager proposal",
            author_id=proposal.proposer_id,
            kind="EDIT",
            request_id=uid(),
        )
        assert proposal.current_revision == 2 and proposal.version == 2
        assert revision_for(db, proposal).award["pool_charge_minor"] == "220000"
        assert revision_for(db, proposal, 1).award["pool_charge_minor"] == "0"
    pools = {pool["manager_id"]: pool for pool in pool_totals(db, cycle)}
    assert pools["manager-a"]["utilized_minor"] == "220000"
    assert pools["manager-b"]["utilized_minor"] == "440000"
    assert pools["manager-b"]["exceptions"] == ["POOL_HOLDBACK_RELEASE"]
    assert pools["manager-b"]["holdback_required_minor"] == "40000"
    worker_verify(db, cycle)
    assert db.scalar(select(func.count()).select_from(CompRevisionVerification)) == 3


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE comp_cycle SET title='altered',version=version+1 WHERE id=:id",
        "UPDATE comp_cycle SET snapshot_id=:old_snapshot,version=version+1 WHERE id=:id",
        "UPDATE comp_cycle SET state='LOCKED',version=version+1 WHERE id=:id",
        "UPDATE comp_cycle SET state='FINAL_APPROVAL',version=version+1 WHERE id=:id",
        "DELETE FROM comp_cycle WHERE id=:id",
        "UPDATE comp_pool SET budget_minor=0 WHERE cycle_id=:id",
        "DELETE FROM comp_pool WHERE cycle_id=:id",
        "UPDATE comp_eligibility SET proposer_id='manager-a' WHERE cycle_id=:id",
        "DELETE FROM comp_eligibility WHERE cycle_id=:id",
        "UPDATE comp_proposal SET proposer_id='hr',version=version+1 WHERE cycle_id=:id",
        "UPDATE comp_proposal SET state='APPROVED',version=version+1 WHERE cycle_id=:id",
        "UPDATE comp_proposal SET current_revision=current_revision+2,version=version+1 WHERE cycle_id=:id",
        "DELETE FROM comp_proposal WHERE cycle_id=:id",
        "UPDATE comp_proposal_revision SET merit_minor=1 WHERE proposal_id IN (SELECT id FROM comp_proposal WHERE cycle_id=:id)",
        "DELETE FROM comp_proposal_revision WHERE proposal_id IN (SELECT id FROM comp_proposal WHERE cycle_id=:id)",
        "UPDATE comp_approval_step SET assigned_id='manager-a' WHERE proposal_id IN (SELECT id FROM comp_proposal WHERE cycle_id=:id)",
        "DELETE FROM comp_approval_step WHERE proposal_id IN (SELECT id FROM comp_proposal WHERE cycle_id=:id)",
    ],
)
def test_frozen_cycle_eligibility_awards_and_steps_cannot_be_changed_directly(db, sql):
    cycle = launched(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(sql), {"id": cycle.id, "old_snapshot": uid()})
    assert check_inputs(db, cycle)["snapshot_id"] == CURRENT_SNAPSHOT
    assert cycle.state == "OPEN"


@pytest.mark.security
def test_independent_verification_rejects_wrong_event_source_and_revision_binding(db):
    cycle = launched(db)
    proposal = db.scalar(select(CompProposal).where(CompProposal.cycle_id == cycle.id))
    revision = revision_for(db, proposal)
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    from meridian.finance_engine import FinanceInputError

    for args in [
        (proposal.id, revision.id, cycle.id, uid()),
        (uid(), revision.id, cycle.id, snapshot.source_batch_id),
        (proposal.id, uid(), cycle.id, snapshot.source_batch_id),
    ]:
        with pytest.raises(FinanceInputError, match="SOURCE_BINDING_MISMATCH"):
            verify_revision(db, *args)
    assert db.scalar(select(func.count()).select_from(CompRevisionVerification)) == 0
