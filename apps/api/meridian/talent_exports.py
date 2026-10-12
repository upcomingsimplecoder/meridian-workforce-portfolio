"""Independent verification of locked human review evidence and private exports."""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.governance import record
from meridian.object_store import put_private, read_private, signing_key
from meridian.people_models import AuthzPolicy, Snapshot
from meridian.policy import canonical_hash
from meridian.talent_models import (
    CalibrationEvent,
    PacketRevision,
    ReviewPacket,
    TalentComment,
    TalentCycle,
    TalentCycleAction,
    TalentDecision,
    TalentExport,
    TalentLock,
)
from meridian.talent_service import checklist, policy_for, revision_document, verified_revision

SIGNING_CONTEXT = b"meridian.talent.packet_export.v1\x00"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def signed(manifest: dict[str, Any]) -> dict[str, Any]:
    key = signing_key()
    return {
        "manifest": manifest,
        "signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "digest": hmac.new(
                key, SIGNING_CONTEXT + canonical_bytes(manifest), hashlib.sha256
            ).hexdigest(),
            "scope": "local artifact authenticity; not a human digital signature or external attestation",
        },
    }


def signature_valid(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"manifest", "signature"}
        or not isinstance(value["manifest"], dict)
    ):
        return False
    expected = signed(value["manifest"])["signature"]
    actual = value["signature"]
    return (
        isinstance(actual, dict)
        and actual.keys() == expected.keys()
        and all(
            isinstance(actual[k], str) and hmac.compare_digest(actual[k], v)
            for k, v in expected.items()
        )
    )


def verify_signature_authority(
    db: Session, packet: ReviewPacket, row: TalentDecision, role: str
) -> None:
    document = row.authorization
    policy = db.get(AuthzPolicy, document.get("policy_id"))
    expected_actor = packet.calibrator_id if role == "reviews.approve" else packet.hr_signer_id
    if (
        policy is None
        or policy.sha256 != canonical_hash(policy.definition)
        or policy.sha256 != document.get("policy_sha256")
        or row.actor_id != expected_actor
        or row.actor_id == packet.author_id
        or document.get("principal_id") != row.actor_id
        or not row.acknowledgement
    ):
        raise DomainError(
            503, "talent_signature_unverifiable", "A frozen human signature cannot be verified."
        )
    for capability in (role, "people.read"):
        permitted = False
        for grant in document.get("grants", []):
            begins = datetime.fromisoformat(grant["effective_from"])
            ends = datetime.fromisoformat(grant["effective_to"]) if grant["effective_to"] else None
            if begins > row.created_at or ends is not None and ends <= row.created_at:
                continue
            if capability not in policy.definition.get(grant["role"], []):
                continue
            if grant["role"] != "manager" and grant["scope"] in {"*", packet.context["org_id"]}:
                permitted = True
            if grant["role"] == "manager" and document.get("manager_root") in packet.chain_ids[1:]:
                permitted = True
        if not permitted:
            raise DomainError(
                503,
                "talent_signature_scope_unverifiable",
                "A historical signature lacks verifiable scoped authority.",
            )


def build_export(db: Session, cycle: TalentCycle, locked: TalentLock) -> dict[str, Any]:
    policy = policy_for(db, cycle)
    if locked.sha256 != canonical_hash(locked.document) or locked.cycle_id != cycle.id:
        raise DomainError(
            503, "talent_lock_fingerprint_mismatch", "The frozen lock cannot be verified."
        )
    refs = locked.document["packets"]
    if (
        locked.document["inputs_sha256"] != cycle.inputs_sha256
        or locked.document["row_count"] != len(refs)
        or len({r["packet_id"] for r in refs}) != len(refs)
    ):
        raise DomainError(
            503, "talent_lock_count_mismatch", "The frozen lock population does not reconcile."
        )
    rows = []
    for ref in refs:
        packet = db.get(ReviewPacket, ref["packet_id"])
        revision = db.get(PacketRevision, ref["revision_id"])
        if (
            packet is None
            or packet.cycle_id != cycle.id
            or revision is None
            or revision.packet_id != packet.id
            or revision.sha256 != ref["sha256"]
        ):
            raise DomainError(
                503,
                "talent_locked_revision_unavailable",
                "A locked packet revision is unavailable.",
            )
        review = verified_revision(db, cycle, packet, revision)
        if checklist(review):
            raise DomainError(
                503, "talent_locked_review_incomplete", "A locked human review is incomplete."
            )
        history = list(
            db.scalars(
                select(PacketRevision)
                .where(
                    PacketRevision.packet_id == packet.id,
                    PacketRevision.number <= revision.number,
                )
                .order_by(PacketRevision.number)
            )
        )
        previous = None
        for revision_item in history:
            verified_revision(db, cycle, packet, revision_item)
            if (
                revision_item.number != (previous.number + 1 if previous else 1)
                or revision_item.parent_id != (previous.id if previous else None)
                or revision_item.parent_sha256 != (previous.sha256 if previous else None)
            ):
                raise DomainError(
                    503,
                    "talent_revision_chain_mismatch",
                    "Immutable revision lineage cannot be verified.",
                )
            previous = revision_item
        signatures = [db.get(TalentDecision, key) for key in ref["signature_ids"]]
        if len(signatures) != 2 or any(row is None for row in signatures):
            raise DomainError(
                503, "talent_lock_signature_missing", "A locked signature is unavailable."
            )
        verified_signatures: list[dict[str, Any]] = []
        for index, signature in enumerate(signatures):
            if (
                signature is None
                or signature.packet_id != packet.id
                or signature.revision_id != revision.id
                or signature.action != ("ACK_CALIBRATION" if index == 0 else "APPROVE")
            ):
                raise DomainError(
                    503,
                    "talent_lock_signature_binding",
                    "A locked signature binds a different revision.",
                )
            verify_signature_authority(
                db, packet, signature, "reviews.approve" if index == 0 else "reviews.manage"
            )
            verified_signatures.append(
                {
                    "id": signature.id,
                    "action": signature.action,
                    "actor_id": signature.actor_id,
                    "revision_id": signature.revision_id,
                    "reason": signature.reason,
                    "acknowledgement": signature.acknowledgement,
                    "authorization": signature.authorization,
                    "request_id": signature.request_id,
                    "at": signature.created_at.isoformat(),
                }
            )
        if datetime.fromisoformat(verified_signatures[0]["at"]) > datetime.fromisoformat(
            verified_signatures[1]["at"]
        ):
            raise DomainError(
                503,
                "talent_signature_order_invalid",
                "Human signature chronology cannot be verified.",
            )
        calibrations = list(
            db.scalars(
                select(CalibrationEvent)
                .join(PacketRevision, CalibrationEvent.revision_id == PacketRevision.id)
                .where(
                    PacketRevision.packet_id == packet.id,
                    PacketRevision.number <= revision.number,
                )
                .order_by(PacketRevision.number)
            )
        )
        for calibration in calibrations:
            before = db.get(PacketRevision, calibration.before_revision_id)
            after = db.get(PacketRevision, calibration.revision_id)
            if (
                before is None
                or after is None
                or after.parent_id != before.id
                or before.sha256 != calibration.before_sha256
                or after.sha256 != calibration.after_sha256
                or after.kind != "CALIBRATE"
                or after.author_id != calibration.actor_id
                or after.reason != calibration.reason
            ):
                raise DomainError(
                    503,
                    "talent_calibration_lineage_invalid",
                    "Calibration before/after evidence cannot be verified.",
                )
        comments = [db.get(TalentComment, key) for key in ref["comment_ids"]]
        comment_rows = []
        for comment in comments:
            if (
                comment is None
                or comment.packet_id != packet.id
                or comment.sha256
                != canonical_hash(
                    {
                        "packet_id": packet.id,
                        "revision_id": comment.revision_id,
                        "author_id": comment.author_id,
                        "body": comment.body,
                    }
                )
            ):
                raise DomainError(
                    503,
                    "talent_comment_fingerprint_invalid",
                    "A frozen comment cannot be verified.",
                )
            comment_rows.append(
                {
                    "id": comment.id,
                    "revision_id": comment.revision_id,
                    "author_id": comment.author_id,
                    "body": comment.body,
                    "sha256": comment.sha256,
                }
            )
        rows.append(
            {
                "packet_id": packet.id,
                "employee_id": packet.employee_id,
                "author_id": packet.author_id,
                "calibrator_id": packet.calibrator_id,
                "hr_signer_id": packet.hr_signer_id,
                "context": packet.context,
                "context_sha256": packet.context_sha256,
                "revision_id": revision.id,
                "revision_sha256": revision.sha256,
                "revision": revision_document(cycle, packet, revision),
                "signatures": verified_signatures,
                "comments": comment_rows,
                "calibrations": [
                    {
                        "revision_id": c.revision_id,
                        "before_revision_id": c.before_revision_id,
                        "actor_id": c.actor_id,
                        "reason": c.reason,
                        "before_sha256": c.before_sha256,
                        "after_sha256": c.after_sha256,
                    }
                    for c in calibrations
                ],
            }
        )
    if len(rows) != len(cycle.assignments) or [r["employee_id"] for r in rows] != sorted(
        a["employee_id"] for a in cycle.assignments
    ):
        raise DomainError(
            503,
            "talent_export_population_mismatch",
            "Locked export population does not reconcile with launch.",
        )
    snapshot = db.get(Snapshot, cycle.snapshot_id)
    if snapshot is None:
        raise DomainError(503, "talent_source_unavailable", "The pinned source is unavailable.")
    return signed(
        {
            "schema_version": 1,
            "kind": "talent.packet_export.v1",
            "cycle_id": cycle.id,
            "lock_id": locked.id,
            "lock_number": locked.number,
            "lock_sha256": locked.sha256,
            "row_count": len(rows),
            "snapshot_id": cycle.snapshot_id,
            "snapshot_sha256": cycle.snapshot_sha256,
            "source_batch_id": snapshot.source_batch_id,
            "policy_id": cycle.policy_id,
            "policy_sha256": policy.sha256,
            "authz_policy_id": cycle.authz_policy_id,
            "inputs_sha256": cycle.inputs_sha256,
            "rows": rows,
        }
    )


def verified_export(
    db: Session, cycle: TalentCycle, locked: TalentLock
) -> tuple[TalentExport, bytes]:
    artifact = db.get(TalentExport, locked.id)
    if artifact is None:
        raise DomainError(
            409, "talent_export_pending", "The independent worker has not completed this export."
        )
    try:
        data = read_private(artifact.storage_key)
        document = json.loads(data)
    except (OSError, ValueError) as error:
        raise DomainError(
            503, "talent_export_integrity_failed", "The private review artifact cannot be verified."
        ) from error
    if (
        not signature_valid(document)
        or artifact.sha256 != canonical_hash(document)
        or data != canonical_bytes(build_export(db, cycle, locked))
        or artifact.row_count != locked.document["row_count"]
    ):
        raise DomainError(
            503,
            "talent_export_integrity_failed",
            "The private review artifact does not reconcile with its frozen lock.",
        )
    return artifact, data


def write_export(
    db: Session,
    cycle_id: str,
    lock_id: str,
    source_batch_id: str,
    *,
    aggregate_version: int | None = None,
) -> TalentExport:
    cycle = db.get(TalentCycle, cycle_id)
    locked = db.get(TalentLock, lock_id)
    snapshot = db.get(Snapshot, cycle.snapshot_id) if cycle else None
    if (
        cycle is None
        or locked is None
        or locked.cycle_id != cycle.id
        or snapshot is None
        or snapshot.source_batch_id != source_batch_id
    ):
        raise DomainError(
            503, "talent_event_binding_invalid", "The review event lacks exact pinned provenance."
        )
    action = db.get(TalentCycleAction, locked.cycle_action_id)
    if action is None or (
        aggregate_version is not None and aggregate_version != action.expected_version + 1
    ):
        raise DomainError(503, "talent_event_version_invalid", "The lock version does not match.")
    existing = db.get(TalentExport, locked.id)
    if existing:
        return verified_export(db, cycle, locked)[0]
    document = build_export(db, cycle, locked)
    data = canonical_bytes(document)
    if len(data) > 8388608:
        raise DomainError(
            503, "talent_export_work_limit", "Review artifact exceeds the sealed work budget."
        )
    artifact = TalentExport(
        lock_id=locked.id,
        storage_key=put_private(data),
        sha256=canonical_hash(document),
        row_count=document["manifest"]["row_count"],
    )
    db.add(artifact)
    db.flush()
    record(
        db,
        None,
        "talent.export_created",
        "talent_cycle",
        cycle.id,
        action.request_id,
        before_hash=locked.sha256,
        after_hash=artifact.sha256,
        emit=False,
        entity_version=locked.number,
    )
    return artifact
