"""Exact pinned reruns; append attempts, never replace missing versions with current ones."""

import hashlib
import hmac
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from meridian.compensation_engine import calculate_award
from meridian.compensation_models import (
    CompApprovalStep,
    CompDecision,
    CompProposal,
    CompProposalRevision,
)
from meridian.compensation_repository import load_guideline
from meridian.compensation_service import check_inputs, checked_award, frozen_for, terms_for
from meridian.errors import DomainError
from meridian.finance_engine import FinanceInputError
from meridian.finance_repository import exact_decimal, load_fx, load_policy
from meridian.governance import record
from meridian.models import AuditEvent, OutboxEvent, uid
from meridian.object_store import put_private, read_private, signing_key
from meridian.people_models import Snapshot
from meridian.policy import canonical_hash
from meridian.replay_auth import Subject
from meridian.replay_material import (
    engine_fingerprint,
    event_document,
    freeze_material,
    iso,
    source_lineage,
)
from meridian.replay_models import ReplayArtifact, ReplayAttempt, ReplayCapsule
from meridian.scenario_models import Scenario, ScenarioDecision, ScenarioRevision
from meridian.scenario_service import calculate_revision, stored_forecast

SIGNING_CONTEXT = b"meridian.redacted.replay.v1\x00"


def canonical_bytes(document: Any) -> bytes:
    import json

    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def actor_reference(actor_id: str | None) -> str | None:
    return canonical_hash({"actor_id": actor_id})[:16] if actor_id is not None else None


def signed_packet(document: dict[str, Any]) -> dict[str, Any]:
    key = signing_key()
    return {
        "evidence": document,
        "signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "digest": hmac.new(
                key, SIGNING_CONTEXT + canonical_bytes(document), hashlib.sha256
            ).hexdigest(),
            "scope": "local artifact authenticity; not tamper-proof or an external human attestation",
        },
    }


def verify_packet(document: dict[str, Any]) -> bool:
    if (
        set(document) != {"evidence", "signature"}
        or not isinstance(document.get("evidence"), dict)
        or not isinstance(document.get("signature"), dict)
    ):
        return False
    expected = signed_packet(document["evidence"])
    signature = document.get("signature", {})
    return (
        signature == expected["signature"]
        and isinstance(signature.get("digest"), str)
        and hmac.compare_digest(signature["digest"], expected["signature"]["digest"])
    )


def pinned_integrity(db: Session, capsule: ReplayCapsule) -> list[str]:
    document = capsule.document
    if capsule.sha256 != canonical_hash(document):
        return ["CAPSULE_FINGERPRINT_MISMATCH"]
    issues = []
    snapshot = db.get(Snapshot, capsule.snapshot_id)
    if snapshot is None or snapshot.status != "PUBLISHED":
        issues.append("PINNED_SNAPSHOT_MISSING_OR_UNSEALED")
    elif snapshot.sha256 != document["snapshot_sha256"]:
        issues.append("PINNED_SNAPSHOT_FINGERPRINT_MISMATCH")
    try:
        if engine_fingerprint(capsule.kind) != document["engine_code_sha256"]:
            issues.append("UNSUPPORTED_PINNED_ENGINE_CODE")
        pins = document["inputs"]
        if load_fx(db, pins["fx_set_id"]).sha256 != pins["fx_sha256"]:
            issues.append("PINNED_FX_FINGERPRINT_MISMATCH")
        if capsule.kind == "SCENARIO":
            if load_policy(db, pins["policy_id"]).sha256 != pins["policy_sha256"]:
                issues.append("PINNED_POLICY_FINGERPRINT_MISMATCH")
        elif load_guideline(db, pins["guideline_id"]).sha256 != pins["guideline_sha256"]:
            issues.append("PINNED_GUIDELINE_FINGERPRINT_MISMATCH")
    except FinanceInputError as error:
        issues.append(str(error))
    for anchor in document["anchors"]:
        row = (
            db.get(AuditEvent, anchor["id"])
            if anchor["kind"] == "AUDIT"
            else db.get(OutboxEvent, anchor["id"])
        )
        if row is None:
            issues.append(anchor["kind"] + "_EVENT_MISSING")
        elif canonical_hash(event_document(row)) != anchor["sha256"]:
            issues.append(anchor["kind"] + "_HASH_MISMATCH")
    try:
        if source_lineage(db, capsule.snapshot_id) != document["source_lineage"]:
            issues.append("SOURCE_LINEAGE_FINGERPRINT_MISMATCH")
    except FinanceInputError as error:
        issues.append(str(error))
    for frozen in document["decisions"]:
        decision = (
            db.get(ScenarioDecision, frozen["id"])
            if capsule.kind == "SCENARIO"
            else db.get(CompDecision, frozen["id"])
        )
        if decision is None:
            issues.append("HUMAN_DECISION_MISSING")
        elif (
            decision.revision_id != frozen["revision_id"]
            or decision.actor_id != frozen["actor_id"]
            or decision.action != frozen["action"]
            or decision.reason != frozen["reason"]
            or decision.request_id != frozen["correlation_id"]
            or iso(decision.created_at) != frozen["at"]
        ):
            issues.append("HUMAN_DECISION_FINGERPRINT_MISMATCH")
        elif isinstance(decision, CompDecision) and (
            decision.conflict_check_sha256 != frozen["conflict_sha256"]
            or decision.round_id != frozen["round_id"]
            or decision.acknowledged_exceptions != frozen["exceptions"]
            or decision.ordinal != frozen["ordinal"]
        ):
            issues.append("HUMAN_DECISION_FINGERPRINT_MISMATCH")
        elif (
            isinstance(decision, ScenarioDecision)
            and decision.expected_version != frozen["ordinal"]
        ):
            issues.append("HUMAN_DECISION_FINGERPRINT_MISMATCH")
    if capsule.kind == "AWARD":
        for step in document.get("approval_steps", []):
            stored = db.get(CompApprovalStep, (capsule.subject_id, step["ordinal"]))
            if (
                stored is None
                or stored.role != step["role"]
                or stored.assigned_id != step["assigned_id"]
                or stored.capability != step["capability"]
                or stored.exception_codes != step["exceptions"]
            ):
                issues.append("APPROVAL_HIERARCHY_FINGERPRINT_MISMATCH")
    return sorted(set(issues))


def reproduce(db: Session, capsule: ReplayCapsule, subject: Subject) -> str:
    if engine_fingerprint(capsule.kind) != capsule.document["engine_code_sha256"]:
        raise FinanceInputError("UNSUPPORTED_PINNED_ENGINE_CODE")
    if capsule.kind == "SCENARIO":
        if not isinstance(subject.entity, Scenario) or not isinstance(
            subject.revision, ScenarioRevision
        ):
            raise FinanceInputError("PINNED_SCENARIO_SUBJECT_MISMATCH")
        stored = stored_forecast(db, capsule.subject_id)
        rerun = calculate_revision(db, subject.entity, subject.revision)
        if (
            canonical_hash(rerun) != stored.sha256
            or stored.sha256 != capsule.document["expected_sha256"]
        ):
            raise FinanceInputError("FORECAST_REPLAY_HASH_MISMATCH")
        baseline_revision = db.get(ScenarioRevision, capsule.document["baseline_revision_id"])
        baseline_scenario = (
            db.get(Scenario, baseline_revision.scenario_id) if baseline_revision else None
        )
        if baseline_revision is None or baseline_scenario is None:
            raise FinanceInputError("PINNED_BASELINE_MISSING")
        baseline = calculate_revision(db, baseline_scenario, baseline_revision)
        if canonical_hash(baseline) != capsule.document["expected_baseline_sha256"]:
            raise FinanceInputError("BASELINE_REPLAY_HASH_MISMATCH")
        return canonical_hash(rerun)
    if (
        subject.cycle is None
        or not isinstance(subject.entity, CompProposal)
        or not isinstance(subject.revision, CompProposalRevision)
    ):
        raise FinanceInputError("PINNED_AWARD_SUBJECT_MISMATCH")
    cycle, proposal, revision = subject.cycle, subject.entity, subject.revision
    check_inputs(db, cycle)
    expected = checked_award(db, cycle, proposal, revision)
    rerun_award = calculate_award(
        frozen_for(db, cycle, proposal.employee_id),
        load_guideline(db, cycle.guideline_id),
        terms_for(cycle),
        load_fx(db, cycle.fx_set_id),
        merit_minor=exact_decimal(revision.merit_minor),
        bonus_minor=exact_decimal(revision.bonus_minor),
    )
    if rerun_award != expected or rerun_award["sha256"] != capsule.document["expected_sha256"]:
        raise FinanceInputError("AWARD_REPLAY_HASH_MISMATCH")
    return str(rerun_award["sha256"])


def redacted_document(capsule: ReplayCapsule) -> dict[str, Any]:
    doc = capsule.document
    return {
        "schema_version": 1,
        "kind": capsule.kind,
        "subject_id": capsule.subject_id,
        "capsule_id": capsule.id,
        "capsule_sha256": capsule.sha256,
        "snapshot_id": capsule.snapshot_id,
        "snapshot_sha256": doc["snapshot_sha256"],
        "source_watermark": doc["source_watermark"],
        "engine_version": doc["engine_version"],
        "model_version": doc["model_version"],
        "engine_code_sha256": doc["engine_code_sha256"],
        "expected_sha256": doc["expected_sha256"],
        "pins": doc["inputs"],
        "aggregate": doc["aggregate"] if doc.get("aggregate_safe", False) else None,
        "baseline_revision_id": doc.get("baseline_revision_id"),
        "expected_baseline_sha256": doc.get("expected_baseline_sha256"),
        "source_lineage": [
            {key: value for key, value in row.items() if key != "publisher_id"}
            for row in doc["source_lineage"]
        ],
        "operations": [
            {
                "id": op["id"],
                "kind": op["kind"],
                "effective_date": op["effective_date"],
                "sha256": canonical_hash(op),
            }
            for op in doc["operations"]
        ],
        "decisions": [
            {
                "id": row["id"],
                "revision_id": row["revision_id"],
                "action": row["action"],
                "ordinal": row["ordinal"],
                "actor_reference": actor_reference(row["actor_id"]),
                "reason_sha256": canonical_hash(row["reason"]),
                "correlation_id": row["correlation_id"],
                "at": row["at"],
            }
            for row in doc["decisions"]
        ],
        "event_anchors": [
            {
                "kind": row["kind"],
                "id": row["id"],
                "sha256": row["sha256"],
                "verb": row["event"]["verb"],
                "at": row["event"]["at"],
                "correlation_id": row["event"]["correlation_id"],
            }
            for row in doc["anchors"]
        ],
        "approval_steps": [
            {
                "ordinal": row["ordinal"],
                "role": row["role"],
                "assigned_reference": actor_reference(row["assigned_id"]),
                "capability": row["capability"],
                "exceptions": row["exceptions"],
            }
            for row in doc.get("approval_steps", [])
        ],
        "boundaries": "Redacted evidence; personnel, individual awards, operation salaries and narratives withheld. No daily external digest or tamper-proof claim.",
    }


def append_attempt(
    db: Session,
    capsule: ReplayCapsule,
    subject: Subject,
    actor_id: str,
    reason: str,
    request_id: str,
) -> ReplayAttempt:
    issues = pinned_integrity(db, capsule)
    observed = None
    if not issues:
        try:
            observed = reproduce(db, capsule, subject)
        except FinanceInputError as error:
            issues = [str(error)]
        except (ValueError, KeyError, TypeError):
            issues = ["PINNED_SOURCE_SCHEMA_CORRUPT"]
    status = (
        "MATCH"
        if not issues
        else "UNSUPPORTED"
        if any("UNSUPPORTED" in value for value in issues)
        else "MISSING"
        if any("MISSING" in value or "UNAVAILABLE" in value for value in issues)
        else "CORRUPT"
    )
    number = (
        db.scalar(
            select(func.max(ReplayAttempt.number)).where(ReplayAttempt.capsule_id == capsule.id)
        )
        or 0
    ) + 1
    attempt = ReplayAttempt(
        id=uid(),
        capsule_id=capsule.id,
        number=number,
        actor_id=actor_id,
        status=status,
        expected_sha256=capsule.document["expected_sha256"],
        observed_sha256=observed,
        issues=issues,
        reason=reason,
        request_id=request_id,
    )
    db.add(attempt)
    db.flush()
    if status == "MATCH":
        document = signed_packet(
            redacted_document(capsule)
            | {
                "attempt_id": attempt.id,
                "observed_sha256": observed,
                "verified_at": iso(attempt.created_at),
            }
        )
        key = put_private(canonical_bytes(document))
        db.add(
            ReplayArtifact(
                attempt_id=attempt.id,
                storage_key=key,
                sha256=canonical_hash(document),
                document=document,
            )
        )
    record(
        db,
        actor_id,
        "governance.replay_recorded",
        "replay_capsule",
        capsule.id,
        request_id,
        after_hash=observed,
        entity_version=number,
        event_payload={
            "entity_type": "replay_capsule",
            "entity_id": capsule.id,
            "attempt_id": attempt.id,
            "capsule_sha256": capsule.sha256,
        },
    )
    db.flush()
    return attempt


def create_capsule(
    db: Session, subject: Subject, actor_id: str, reason: str, request_id: str
) -> ReplayCapsule:
    document = freeze_material(db, subject)
    capsule = ReplayCapsule(
        id=uid(),
        kind=subject.kind,
        subject_id=subject.revision.id,
        snapshot_id=subject.snapshot_id,
        owner_id=actor_id,
        document=document,
        sha256=canonical_hash(document),
        reason=reason,
        request_id=request_id,
    )
    db.add(capsule)
    db.flush()
    append_attempt(db, capsule, subject, actor_id, reason, request_id)
    return capsule


def artifact_integrity(db: Session, capsule: ReplayCapsule, attempt: ReplayAttempt) -> bytes:
    if (
        attempt.capsule_id != capsule.id
        or attempt.status != "MATCH"
        or attempt.issues
        or attempt.expected_sha256 != capsule.document["expected_sha256"]
        or attempt.expected_sha256 != attempt.observed_sha256
        or pinned_integrity(db, capsule)
    ):
        raise DomainError(
            503, "replay_evidence_unverified", "The frozen evidence cannot currently be verified."
        )
    artifact = db.get(ReplayArtifact, attempt.id)
    if artifact is None or artifact.sha256 != canonical_hash(artifact.document):
        raise DomainError(
            503, "replay_artifact_corrupt", "The signed evidence artifact is missing or corrupt."
        )
    try:
        signed = verify_packet(artifact.document)
    except (OSError, RuntimeError) as error:
        raise DomainError(
            503, "replay_signing_unavailable", "The evidence signing key is unavailable or invalid."
        ) from error
    if not signed:
        raise DomainError(
            503, "replay_artifact_corrupt", "The signed evidence artifact is missing or corrupt."
        )
    try:
        raw = read_private(artifact.storage_key)
    except OSError as error:
        raise DomainError(
            503, "replay_storage_unavailable", "The private evidence artifact is unavailable."
        ) from error
    expected = redacted_document(capsule) | {
        "attempt_id": attempt.id,
        "observed_sha256": attempt.observed_sha256,
        "verified_at": iso(attempt.created_at),
    }
    if raw != canonical_bytes(artifact.document) or artifact.document["evidence"] != expected:
        raise DomainError(
            503,
            "replay_artifact_binding_mismatch",
            "The evidence does not match its frozen attempt.",
        )
    return raw


def verified_packet(
    db: Session, capsule: ReplayCapsule, attempt: ReplayAttempt, subject: Subject
) -> bytes:
    raw = artifact_integrity(db, capsule, attempt)
    try:
        if reproduce(db, capsule, subject) != attempt.observed_sha256:
            raise FinanceInputError("REPLAY_EXPORT_HASH_MISMATCH")
    except (FinanceInputError, ValueError, KeyError, TypeError) as error:
        raise DomainError(
            503,
            "replay_evidence_unverified",
            "The frozen financial inputs cannot currently be reproduced.",
        ) from error
    return raw
