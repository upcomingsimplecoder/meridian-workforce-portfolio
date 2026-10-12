"""Freeze verified source versions and exact, chronologically ordered provenance anchors."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from meridian import compensation_engine, finance_engine, org_calendar, scenario_operations
from meridian.compensation_models import (
    CompApprovalStep,
    CompDecision,
    CompLock,
    CompProposalRevision,
)
from meridian.compensation_service import check_inputs, checked_award, frozen_for
from meridian.finance_engine import FinanceInputError
from meridian.ingestion_models import IngestionInput
from meridian.models import AuditEvent, OutboxEvent
from meridian.people_models import Snapshot, SourceBatch
from meridian.policy import canonical_hash
from meridian.replay_auth import Subject
from meridian.scenario_models import (
    Scenario,
    ScenarioDecision,
    ScenarioRevision,
    ScenarioVerification,
)
from meridian.scenario_service import revision_for, stored_forecast

MAX_ANCHORS = 1500


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def engine_fingerprint(kind: str) -> str:
    modules = (
        (finance_engine, scenario_operations, org_calendar)
        if kind == "SCENARIO"
        else (finance_engine, compensation_engine)
    )
    hashes = {}
    for module in modules:
        if module.__file__ is None:
            raise FinanceInputError("PINNED_ENGINE_CODE_UNAVAILABLE")
        try:
            hashes[module.__name__] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        except OSError as error:
            raise FinanceInputError("PINNED_ENGINE_CODE_UNAVAILABLE") from error
    return canonical_hash(hashes)


def event_document(row: AuditEvent | OutboxEvent) -> dict[str, Any]:
    if isinstance(row, AuditEvent):
        return {
            "id": row.id,
            "kind": "AUDIT",
            "verb": row.verb,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "version": row.entity_version,
            "actor_id": row.actor_id,
            "correlation_id": row.request_id,
            "permission": row.permission_result,
            "policy_version": row.policy_version,
            "before_sha256": row.before_hash,
            "after_sha256": row.after_hash,
            "at": iso(row.occurred_at),
        }
    # Delivery state/retry timestamps are mutable operational metadata, not event identity.
    return {
        "id": row.id,
        "kind": "OUTBOX",
        "verb": row.event_type,
        "entity_id": row.aggregate_id,
        "version": row.aggregate_version,
        "schema_version": row.schema_version,
        "correlation_id": row.request_id,
        "payload": row.payload,
        "at": iso(row.occurred_at),
    }


def source_lineage(db: Session, snapshot_id: str) -> list[dict[str, Any]]:
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.status != "PUBLISHED":
        raise FinanceInputError("PINNED_SNAPSHOT_UNAVAILABLE_OR_UNSEALED")
    lineage = []
    batch_id: str | None = snapshot.source_batch_id
    seen: set[str] = set()
    while batch_id is not None:
        if batch_id in seen or len(seen) >= 64:
            raise FinanceInputError("SOURCE_LINEAGE_CYCLE_OR_LIMIT")
        seen.add(batch_id)
        batch = db.get(SourceBatch, batch_id)
        if batch is None:
            raise FinanceInputError("PINNED_SOURCE_BATCH_MISSING")
        parent = db.get(IngestionInput, batch_id)
        lineage.append(
            {
                "batch_id": batch.id,
                "source_type": batch.source_type,
                "sha256": batch.sha256,
                "accepted_count": batch.accepted_count,
                "rejected_count": batch.rejected_count,
                "schema_version": batch.schema_version,
                "watermark": batch.watermark.isoformat(),
                "supersedes_batch_id": parent.supersedes_batch_id if parent else None,
                "base_snapshot_id": parent.base_snapshot_id if parent else None,
                "published_at": iso(batch.published_at) if batch.published_at else None,
                "publisher_id": batch.actor_id,
            }
        )
        batch_id = parent.supersedes_batch_id if parent else None
    return lineage


def scenario_parts(db: Session, subject: Subject) -> dict[str, Any]:
    if not isinstance(subject.entity, Scenario) or not isinstance(
        subject.revision, ScenarioRevision
    ):
        raise FinanceInputError("PINNED_SCENARIO_SUBJECT_MISMATCH")
    scenario, revision = subject.entity, subject.revision
    current = stored_forecast(db, revision.id)
    verified = db.get(ScenarioVerification, revision.id)
    if verified is None or verified.sha256 != current.sha256:
        raise FinanceInputError("PINNED_SCENARIO_WORKER_VERIFICATION_MISSING")
    baseline = stored_forecast(db, scenario.parent_revision_id or revision_for(db, scenario, 1).id)
    baseline_revision = db.get(ScenarioRevision, baseline.revision_id)
    if baseline_revision is None:
        raise FinanceInputError("PINNED_BASELINE_MISSING")
    decision_rows = list(
        db.scalars(
            select(ScenarioDecision)
            .where(ScenarioDecision.revision_id.in_([revision.id, baseline_revision.id]))
            .order_by(ScenarioDecision.created_at, ScenarioDecision.id)
        )
    )
    decisions = [
        {
            "id": row.id,
            "revision_id": row.revision_id,
            "action": row.action,
            "actor_id": row.actor_id,
            "reason": row.reason,
            "ordinal": row.expected_version,
            "correlation_id": row.request_id,
            "at": iso(row.created_at),
        }
        for row in decision_rows
    ]
    planned, base = current.document["result"], baseline.document["result"]
    rows = []
    for month, original in zip(planned["months"], base["months"], strict=True):
        rows.append(
            {
                "month": month["month"],
                "planned": {key: value for key, value in month.items() if key.endswith("_minor")},
                "baseline": {
                    key: value for key, value in original.items() if key.endswith("_minor")
                },
                "delta_minor": str(int(month["total_minor"]) - int(original["total_minor"])),
            }
        )
    return {
        "expected_sha256": current.sha256,
        "expected_baseline_sha256": baseline.sha256,
        "baseline_revision_id": baseline.revision_id,
        "revision_sha256": revision.sha256,
        "inputs": planned["inputs"],
        "year": scenario.year,
        "scope_orgs": scenario.scope_orgs,
        "engine_version": planned["engine_version"],
        "model_version": planned["model_version"],
        "operations": revision.operations,
        "decisions": decisions,
        "entity_ids": sorted(
            {
                scenario.id,
                baseline_revision.scenario_id,
                *[row["id"] for row in decisions],
                *[row["id"] for row in revision.operations],
            }
        ),
        "revision_ids": sorted({revision.id, baseline_revision.id}),
        "revision_hashes": sorted({revision.sha256, baseline_revision.sha256}),
        "aggregate": {
            "currency": planned["currency"],
            "exponent": planned["exponent"],
            "planned": planned["annual"],
            "baseline": base["annual"],
            "delta_minor": str(
                int(planned["annual"]["total_minor"]) - int(base["annual"]["total_minor"])
            ),
            "months": rows,
        },
        "aggregate_safe": all(
            min(row["opening_headcount"], row["closing_headcount"]) >= 5
            for result in (planned, base)
            for row in result["months"]
        ),
        "full_award": None,
        "hierarchy": [],
    }


def award_parts(db: Session, subject: Subject) -> dict[str, Any]:
    if subject.cycle is None or not isinstance(subject.revision, CompProposalRevision):
        raise FinanceInputError("PINNED_AWARD_SUBJECT_MISMATCH")
    cycle, revision = subject.cycle, subject.revision
    from meridian.compensation_models import CompProposal, CompRevisionVerification

    if not isinstance(subject.entity, CompProposal):
        raise FinanceInputError("PINNED_AWARD_SUBJECT_MISMATCH")
    proposal = subject.entity
    check_inputs(db, cycle)
    frozen = frozen_for(db, cycle, proposal.employee_id)
    award = checked_award(db, cycle, proposal, revision)
    verified = db.get(CompRevisionVerification, revision.id)
    if verified is None or verified.sha256 != revision.sha256:
        raise FinanceInputError("PINNED_AWARD_WORKER_VERIFICATION_MISSING")
    history = list(
        db.scalars(
            select(CompProposalRevision)
            .where(
                CompProposalRevision.proposal_id == proposal.id,
                CompProposalRevision.number <= revision.number,
            )
            .order_by(CompProposalRevision.number)
        )
    )
    decisions = [
        {
            "id": row.id,
            "revision_id": row.revision_id,
            "action": row.action,
            "actor_id": row.actor_id,
            "reason": row.reason,
            "ordinal": row.ordinal,
            "correlation_id": row.request_id,
            "at": iso(row.created_at),
            "round_id": row.round_id,
            "exceptions": row.acknowledged_exceptions,
            "conflict_sha256": row.conflict_check_sha256,
        }
        for row in db.scalars(
            select(CompDecision)
            .where(CompDecision.revision_id.in_([r.id for r in history]))
            .order_by(CompDecision.created_at, CompDecision.id)
        )
    ]
    steps = [
        {
            "ordinal": row.ordinal,
            "role": row.role,
            "assigned_id": row.assigned_id,
            "capability": row.capability,
            "exceptions": row.exception_codes,
        }
        for row in db.scalars(
            select(CompApprovalStep)
            .where(CompApprovalStep.revision_id == revision.id)
            .order_by(CompApprovalStep.ordinal)
        )
    ]
    locked = db.scalar(select(CompLock).where(CompLock.cycle_id == cycle.id))
    return {
        "expected_sha256": award["sha256"],
        "revision_sha256": revision.sha256,
        "inputs": award["inputs"]
        | {
            "snapshot_id": cycle.snapshot_id,
            "guideline_id": cycle.guideline_id,
            "authz_policy_id": cycle.authz_policy_id,
            "cycle_inputs_sha256": cycle.inputs_sha256,
        },
        "engine_version": award["engine_version"],
        "model_version": award["model_version"],
        "scope_orgs": cycle.scope_orgs,
        "operations": [],
        "decisions": decisions,
        "approval_steps": steps,
        "entity_ids": sorted({proposal.id, cycle.id, *[row["id"] for row in decisions]}),
        "revision_ids": [row.id for row in history],
        "revision_hashes": [row.sha256 for row in history],
        "lock_id": locked.id if locked else None,
        "lock_sha256": locked.sha256 if locked else None,
        "aggregate": None,
        "full_award": award,
        "hierarchy": [link.document() for link in frozen.chain],
        "revision_history": [
            {
                "id": row.id,
                "number": row.number,
                "kind": row.kind,
                "sha256": row.sha256,
                "award_sha256": row.award["sha256"],
            }
            for row in history
        ],
        "award_history": [
            {"revision_id": row.id, "number": row.number, "award": row.award} for row in history
        ],
    }


def freeze_material(db: Session, subject: Subject) -> dict[str, Any]:
    parts = scenario_parts(db, subject) if subject.kind == "SCENARIO" else award_parts(db, subject)
    lineage = source_lineage(db, subject.snapshot_id)
    batch_ids = [row["batch_id"] for row in lineage]
    events = list(
        db.scalars(
            select(OutboxEvent)
            .where(
                or_(
                    OutboxEvent.aggregate_id.in_(batch_ids),
                    (
                        OutboxEvent.aggregate_id.in_(parts["entity_ids"])
                        & OutboxEvent.payload["revision_id"].as_string().in_(parts["revision_ids"])
                    ),
                    (
                        OutboxEvent.aggregate_id.in_(parts["entity_ids"])
                        & (OutboxEvent.event_type == "comp.cycle_locked.v1")
                    ),
                )
            )
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .limit(MAX_ANCHORS + 1)
        )
    )
    correlations = {row.request_id for row in events} | {
        row["correlation_id"] for row in parts["decisions"]
    }
    audits = list(
        db.scalars(
            select(AuditEvent)
            .where(
                or_(
                    AuditEvent.request_id.in_(correlations),
                    (
                        AuditEvent.entity_id.in_(parts["entity_ids"])
                        & AuditEvent.after_hash.in_(parts["revision_hashes"])
                    ),
                )
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
            .limit(MAX_ANCHORS + 1)
        )
    )
    if len(events) + len(audits) > MAX_ANCHORS:
        raise FinanceInputError("REPLAY_EVENT_WORK_LIMIT")
    all_events: list[AuditEvent | OutboxEvent] = [*audits, *events]
    anchors: list[dict[str, Any]] = [
        {
            "kind": "AUDIT" if isinstance(row, AuditEvent) else "OUTBOX",
            "id": row.id,
            "sha256": canonical_hash(event_document(row)),
            "event": event_document(row),
        }
        for row in all_events
    ]
    anchors.sort(key=lambda row: (row["event"]["at"], row["kind"], row["id"]))
    snapshot = db.get(Snapshot, subject.snapshot_id)
    if snapshot is None:
        raise FinanceInputError("PINNED_SNAPSHOT_MISSING")
    return {
        "schema_version": 1,
        "kind": subject.kind,
        "subject_id": subject.revision.id,
        "snapshot_id": snapshot.id,
        "snapshot_sha256": snapshot.sha256,
        "source_watermark": snapshot.source_watermark.isoformat(),
        "source_lineage": lineage,
        "engine_code_sha256": engine_fingerprint(subject.kind),
        "anchors": anchors,
        **parts,
    }
