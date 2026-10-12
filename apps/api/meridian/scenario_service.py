"""Transactional scenario revisions and independent, idempotent worker verification."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian import object_store
from meridian.finance_engine import ENGINE_VERSION, FinanceInputError, calculate_forecast
from meridian.finance_repository import load_fx, load_planning_history, load_policy
from meridian.governance import record
from meridian.models import uid
from meridian.org_calendar import OrgCalendar, OrgPeriod
from meridian.people_models import OrgVersion, Snapshot
from meridian.policy import canonical_hash
from meridian.scenario_models import (
    Scenario,
    ScenarioDecision,
    ScenarioForecast,
    ScenarioManifest,
    ScenarioRevision,
    ScenarioVerification,
)
from meridian.scenario_operations import (
    OPERATION_MODEL_VERSION,
    OperationList,
    PlanningRules,
    apply_operations,
    validate_budget,
)


def revision_for(db: Session, scenario: Scenario, number: int | None = None) -> ScenarioRevision:
    revision = db.scalar(
        select(ScenarioRevision).where(
            ScenarioRevision.scenario_id == scenario.id,
            ScenarioRevision.number == (number or scenario.current_revision),
        )
    )
    if revision is None:
        raise FinanceInputError("PINNED_SCENARIO_REVISION_UNAVAILABLE")
    return revision


def revision_hash(scenario: Scenario, revision: ScenarioRevision) -> str:
    return canonical_hash(
        {
            "scenario_id": scenario.id,
            "snapshot_id": scenario.base_snapshot_id,
            "policy_id": scenario.policy_id,
            "fx_set_id": scenario.fx_set_id,
            "year": scenario.year,
            "planning_date": scenario.planning_date.isoformat(),
            "scope_orgs": scenario.scope_orgs,
            "rules": scenario.rules,
            "budget_minor": scenario.budget_minor,
            "parent_revision_id": scenario.parent_revision_id,
            "number": revision.number,
            "operations": revision.operations,
            "author_id": revision.author_id,
            "reason": revision.reason,
        }
    )


def calculate_revision(
    db: Session, scenario: Scenario, revision: ScenarioRevision
) -> dict[str, Any]:
    if revision.sha256 != revision_hash(scenario, revision):
        raise FinanceInputError("SCENARIO_REVISION_FINGERPRINT_MISMATCH")
    rules = PlanningRules(scenario.rules.get("hire_lead_days", -1))
    if rules.document() != scenario.rules:
        raise FinanceInputError("UNSUPPORTED_PINNED_PLANNING_RULES")
    snapshot, source = load_planning_history(db, scenario.base_snapshot_id)
    orgs = list(
        db.scalars(
            select(OrgVersion)
            .where(OrgVersion.snapshot_id == snapshot.id)
            .order_by(OrgVersion.org_id, OrgVersion.valid_from)
        )
    )
    calendar = OrgCalendar(
        tuple(OrgPeriod(row.org_id, row.region, row.valid_from, row.valid_to) for row in orgs)
    )
    transformed = apply_operations(
        source,
        OperationList.validate_python(revision.operations),
        year=scenario.year,
        planning_date=scenario.planning_date,
        scope_orgs=tuple(scenario.scope_orgs),
        org_regions=calendar,
        rules=rules,
        snapshot_source_batch_id=snapshot.source_batch_id,
    )
    result = calculate_forecast(
        [row.cost for row in transformed],
        load_policy(db, scenario.policy_id),
        load_fx(db, scenario.fx_set_id),
        snapshot_id=snapshot.id,
        snapshot_sha256=snapshot.sha256,
        year=scenario.year,
        scope_orgs=tuple(scenario.scope_orgs),
    )
    validate_budget(result, scenario.budget_minor)
    return {
        "scenario_id": scenario.id,
        "revision_id": revision.id,
        "revision_sha256": revision.sha256,
        "operation_model_version": OPERATION_MODEL_VERSION,
        "planning_rules_sha256": canonical_hash(scenario.rules),
        "org_calendar_sha256": canonical_hash(
            [
                {
                    "id": row.id,
                    "org_id": row.org_id,
                    "region": row.region,
                    "from": row.valid_from,
                    "to": row.valid_to,
                }
                for row in orgs
            ]
        ),
        "source_batch_id": snapshot.source_batch_id,
        "result": result,
    }


def scenario_event(
    db: Session,
    scenario: Scenario,
    revision: ScenarioRevision,
    actor_id: str,
    verb: str,
    request_id: str,
    *,
    before_hash: str | None = None,
) -> None:
    snapshot = db.get(Snapshot, scenario.base_snapshot_id)
    if snapshot is None:
        raise FinanceInputError("PINNED_SNAPSHOT_UNAVAILABLE")
    record(
        db,
        actor_id,
        verb,
        "scenario",
        scenario.id,
        request_id,
        before_hash=before_hash,
        after_hash=revision.sha256,
        entity_version=scenario.version,
        event_payload={
            "entity_type": "scenario",
            "entity_id": scenario.id,
            "revision_id": revision.id,
            "source_batch_id": snapshot.source_batch_id,
        },
    )


def save_revision(
    db: Session,
    scenario: Scenario,
    operations: list[dict[str, Any]],
    actor_id: str,
    reason: str,
    request_id: str,
) -> ScenarioRevision:
    if scenario.state not in {"BUILDING", "DRAFT", "REJECTED"} or scenario.current_revision >= 201:
        raise FinanceInputError("SCENARIO_REVISION_LOCKED_OR_LIMIT")
    reason = reason.strip()
    if len(reason) < 3:
        raise FinanceInputError("DECISION_REASON_REQUIRED")
    previous = revision_for(db, scenario) if scenario.current_revision else None
    revision = ScenarioRevision(
        id=uid(),
        scenario_id=scenario.id,
        number=scenario.current_revision + 1,
        operations=operations,
        author_id=actor_id,
        reason=reason,
    )
    revision.sha256 = revision_hash(scenario, revision)
    document = calculate_revision(db, scenario, revision)
    db.add(revision)
    db.flush()
    db.add(
        ScenarioForecast(
            revision_id=revision.id,
            engine_version=ENGINE_VERSION,
            model_version=document["result"]["model_version"],
            document=document,
            sha256=canonical_hash(document),
        )
    )
    db.flush()
    scenario.current_revision = revision.number
    scenario.version += 1
    scenario.state = "DRAFT"
    db.flush()
    scenario_event(
        db,
        scenario,
        revision,
        actor_id,
        "scenario.revision_created",
        request_id,
        before_hash=previous.sha256 if previous else None,
    )
    previous_ops = {op["id"]: op for op in previous.operations} if previous else {}
    current_ops = {op["id"]: op for op in operations}
    for operation_id in sorted(set(previous_ops) ^ set(current_ops)):
        added = operation_id in current_ops
        record(
            db,
            actor_id,
            "scenario.operation_added" if added else "scenario.operation_undone",
            "scenario_operation",
            operation_id,
            request_id,
            before_hash=canonical_hash(previous_ops[operation_id]) if not added else None,
            after_hash=canonical_hash(current_ops[operation_id]) if added else None,
            emit=False,
            entity_version=revision.number,
        )
    return revision


def stored_forecast(db: Session, revision_id: str) -> ScenarioForecast:
    stored = db.get(ScenarioForecast, revision_id)
    if (
        stored is None
        or stored.engine_version != ENGINE_VERSION
        or stored.sha256 != canonical_hash(stored.document)
    ):
        raise FinanceInputError("PINNED_FORECAST_UNAVAILABLE_OR_CORRUPT")
    return stored


def verify_revision(db: Session, scenario_id: str, revision_id: str, source_batch_id: str) -> None:
    scenario, revision = db.get(Scenario, scenario_id), db.get(ScenarioRevision, revision_id)
    if scenario is None or revision is None or revision.scenario_id != scenario.id:
        raise FinanceInputError("EVENT_REVISION_BINDING_MISMATCH")
    stored = stored_forecast(db, revision_id)
    if stored.document["source_batch_id"] != source_batch_id:
        raise FinanceInputError("EVENT_SOURCE_BINDING_MISMATCH")
    if calculate_revision(db, scenario, revision) != stored.document:
        raise FinanceInputError("REPLAY_FINANCIAL_DRIFT")
    existing = db.get(ScenarioVerification, revision_id)
    if existing is None:
        db.add(ScenarioVerification(revision_id=revision_id, sha256=stored.sha256))
    elif existing.sha256 != stored.sha256:
        raise FinanceInputError("REPLAY_VERIFICATION_DRIFT")
    db.flush()


def write_approved_manifest(
    db: Session, scenario_id: str, revision_id: str, source_batch_id: str
) -> None:
    verify_revision(db, scenario_id, revision_id, source_batch_id)
    forecast = stored_forecast(db, revision_id)
    decision = db.scalar(
        select(ScenarioDecision).where(
            ScenarioDecision.scenario_id == scenario_id,
            ScenarioDecision.revision_id == revision_id,
            ScenarioDecision.action == "APPROVE",
        )
    )
    if decision is None:
        raise FinanceInputError("APPROVAL_DECISION_MISSING")
    document = {
        "schema_version": 1,
        "kind": "approved_scenario",
        "scenario_id": scenario_id,
        "revision_id": revision_id,
        "forecast_sha256": forecast.sha256,
        "source_batch_id": source_batch_id,
        "approval": {
            "id": decision.id,
            "actor_id": decision.actor_id,
            "reason": decision.reason,
            "request_id": decision.request_id,
            "at": decision.created_at.isoformat(),
        },
        "forecast": forecast.document,
    }
    serialized = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    key = object_store.put_private(serialized)
    existing = db.get(ScenarioManifest, revision_id)
    if existing is None:
        db.add(
            ScenarioManifest(
                revision_id=revision_id,
                sha256=canonical_hash(document),
                storage_key=key,
                document=document,
            )
        )
    elif existing.sha256 != canonical_hash(document) or existing.storage_key != key:
        raise FinanceInputError("APPROVED_MANIFEST_DRIFT")
    db.flush()
