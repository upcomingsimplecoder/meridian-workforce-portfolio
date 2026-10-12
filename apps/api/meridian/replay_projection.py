"""Current field-level projection of immutable replay material and attempts."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.policy import canonical_hash
from meridian.replay_auth import Subject
from meridian.replay_models import ReplayAttempt, ReplayCapsule
from meridian.replay_schemas import ReplayPin, ReplayView
from meridian.replay_service import actor_reference, pinned_integrity


def pins(document: dict[str, Any]) -> list[ReplayPin]:
    inputs = document["inputs"]
    result = [
        ReplayPin(
            name="Calculation engine",
            version=document["engine_version"],
            sha256=document["engine_code_sha256"],
        ),
        ReplayPin(name="Calculation model", version=document["model_version"]),
        ReplayPin(
            name="Workforce snapshot",
            version=document["snapshot_id"],
            sha256=document["snapshot_sha256"],
        ),
    ]
    for name, key, fingerprint in (
        ("Cost policy", "policy_id", "policy_sha256"),
        ("Pinned FX", "fx_set_id", "fx_sha256"),
        ("Compensation guideline", "guideline_id", "guideline_sha256"),
        ("Cycle authorization policy", "authz_policy_id", None),
    ):
        if key in inputs:
            result.append(
                ReplayPin(
                    name=name,
                    version=inputs[key],
                    sha256=inputs.get(fingerprint) if fingerprint else None,
                )
            )
    for name, key in (
        (
            "Scenario revision" if document["kind"] == "SCENARIO" else "Award revision",
            "revision_sha256",
        ),
        ("Cycle inputs", "cycle_inputs_sha256"),
        ("Eligibility", "eligibility_sha256"),
        ("Compensation terms", "terms_sha256"),
    ):
        fingerprint = document.get(key) or inputs.get(key)
        if fingerprint:
            result.append(ReplayPin(name=name, version="frozen", sha256=fingerprint))
    if document.get("baseline_revision_id"):
        result.append(
            ReplayPin(
                name="Comparison baseline",
                version=document["baseline_revision_id"],
                sha256=document["expected_baseline_sha256"],
            )
        )
    return result


def view(db: Session, capsule: ReplayCapsule, subject: Subject) -> ReplayView:
    document = capsule.document
    if canonical_hash(document) != capsule.sha256:
        raise DomainError(
            503,
            "replay_capsule_corrupt",
            "The immutable evidence capsule fingerprint does not match. Capture and verification are unavailable.",
        )
    issues = pinned_integrity(db, capsule)
    attempts = []
    for row in db.scalars(
        select(ReplayAttempt)
        .where(ReplayAttempt.capsule_id == capsule.id)
        .order_by(ReplayAttempt.number.desc())
        .limit(25)
    ):
        ready = False
        artifact_issues = []
        if not issues and row.status == "MATCH":
            # Read private bytes and bindings without doing an expensive financial rerun
            # per historical attempt. Downloads always reproduce the financial calculation.
            from meridian.replay_service import artifact_integrity

            try:
                artifact_integrity(db, capsule, row)
                ready = True
            except DomainError as error:
                artifact_issues.append(error.code)
        attempts.append(
            {
                "id": row.id,
                "number": row.number,
                "status": row.status,
                "expected_sha256": row.expected_sha256,
                "observed_sha256": row.observed_sha256,
                "issues": row.issues,
                "request_id": row.request_id,
                "created_at": row.created_at,
                "export_ready": ready,
                "artifact_issues": artifact_issues,
            }
        )
    decisions = [
        {
            "id": row["id"],
            "revision_id": row["revision_id"],
            "action": row["action"],
            "actor_reference": actor_reference(row["actor_id"]),
            "ordinal": row["ordinal"],
            "correlation_id": row["correlation_id"],
            "at": row["at"],
            "reason_sha256": canonical_hash(row["reason"]),
            "reason": row["reason"] if not subject.redacted else None,
            "exceptions": row.get("exceptions", []),
        }
        for row in document["decisions"]
    ]
    timeline = []
    for anchor in document["anchors"]:
        event = anchor["event"]
        timeline.append(
            {
                "id": anchor["id"],
                "kind": anchor["kind"],
                "verb": event["verb"],
                "at": event["at"],
                "correlation_id": event["correlation_id"],
                "sha256": anchor["sha256"],
                "before_sha256": event.get("before_sha256"),
                "after_sha256": event.get("after_sha256"),
                "policy_version": event.get("policy_version"),
                "actor_reference": actor_reference(event.get("actor_id")),
            }
        )
    return ReplayView.model_validate(
        {
            "id": capsule.id,
            "kind": capsule.kind,
            "subject_id": capsule.subject_id,
            "snapshot_id": capsule.snapshot_id,
            "snapshot_sha256": document["snapshot_sha256"],
            "source_watermark": document["source_watermark"],
            "created_at": capsule.created_at,
            "sha256": capsule.sha256,
            "redacted": subject.redacted,
            "aggregate": document["aggregate"]
            if not subject.redacted or document.get("aggregate_safe", False)
            else None,
            "award": document["full_award"] if not subject.redacted else None,
            "full_operations": document["operations"] if not subject.redacted else [],
            "operations": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "effective_date": row["effective_date"],
                    "sha256": canonical_hash(row),
                }
                for row in document["operations"]
            ],
            "decisions": decisions,
            "timeline": timeline,
            "source_lineage": [
                {key: value for key, value in row.items() if key != "publisher_id"}
                for row in document["source_lineage"]
            ],
            "pins": pins(document),
            "attempts": attempts,
            "integrity_issues": issues,
            "hierarchy": [
                {
                    "version_id": row["version_id"],
                    "source_batch_id": row["source_batch_id"],
                    "person_reference": str(actor_reference(row["employee_id"])),
                    "manager_reference": str(actor_reference(row["manager_id"])),
                }
                for row in document["hierarchy"]
            ],
            "approval_steps": [
                {
                    "ordinal": row["ordinal"],
                    "role": row["role"],
                    "assigned_reference": actor_reference(row["assigned_id"]),
                    "capability": row["capability"],
                    "exceptions": row["exceptions"],
                }
                for row in document.get("approval_steps", [])
            ],
            "revision_history": document.get("revision_history", []),
            "award_history": document.get("award_history", []) if not subject.redacted else [],
        }
    )
