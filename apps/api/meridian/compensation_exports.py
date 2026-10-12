"""Counted immutable local exports, HMAC manifests and source/file reconciliation."""

import csv
import hashlib
import hmac
import io
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.compensation_approvals import finance_document
from meridian.compensation_models import (
    CompCycle,
    CompCycleAction,
    CompExportArtifact,
    CompLock,
    CompReconciliation,
)
from meridian.compensation_service import (
    frozen_for,
    latest_rows,
    record_cycle_action,
    terms_for,
    verify_revision,
)
from meridian.errors import DomainError
from meridian.finance_engine import EXPONENTS, FinanceInputError
from meridian.finance_repository import exact_decimal
from meridian.governance import record
from meridian.object_store import MAX_UPLOAD_BYTES, put_private, read_private, signing_key
from meridian.people import safe_cell
from meridian.people_models import Snapshot
from meridian.policy import canonical_hash

CSV_COLUMNS = (
    "employee_id",
    "org_id",
    "cost_center",
    "currency",
    "currency_exponent",
    "merit_effective_date",
    "base_pay_full_time_minor",
    "merit_full_time_increment_minor",
    "new_base_pay_full_time_minor",
    "fte_at_merit_effective_date",
    "annualized_merit_cost_minor",
    "first_year_merit_cost_minor",
    "bonus_payment_date",
    "bonus_target_full_time_minor",
    "bonus_payable_minor",
    "pool_charge_minor",
    "proposal_revision",
    "snapshot_id",
    "guideline_id",
    "fx_set_id",
)
SUM_FIELDS = {
    "annualized_merit_cost_minor": "annualized_merit_minor",
    "first_year_merit_cost_minor": "merit_first_year_minor",
    "bonus_payable_minor": "bonus_payable_minor",
    "pool_charge_minor": "pool_charge_minor",
}
SIGNING_CONTEXT = b"meridian.comp.manifest.v1\x00"


def canonical_bytes(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def signed_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
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


def verify_manifest(document: dict[str, Any]) -> bool:
    if set(document) != {"manifest", "signature"}:
        return False
    expected = signed_manifest(document["manifest"])
    signature = document.get("signature", {})
    return (
        signature.get("algorithm") == "HMAC-SHA256"
        and signature.get("key_id") == expected["signature"]["key_id"]
        and isinstance(signature.get("digest"), str)
        and hmac.compare_digest(signature["digest"], expected["signature"]["digest"])
        and signature == expected["signature"]
    )


def csv_for(db: Session, cycle: CompCycle, locked: CompLock) -> bytes:
    if locked.sha256 != canonical_hash(locked.document):
        raise FinanceInputError("COMPENSATION_LOCK_FINGERPRINT_MISMATCH")
    document = finance_document(db, cycle)
    if any(document[field] != locked.document[field] for field in document):
        raise FinanceInputError("COMPENSATION_LOCKED_FINANCIAL_BUNDLE_CHANGED")
    pools = {pool["id"]: pool for pool in document["pools"]}
    from meridian.compensation_models import CompEligibility

    memberships = {
        row.employee_id: row.pool_id
        for row in db.scalars(select(CompEligibility).where(CompEligibility.cycle_id == cycle.id))
    }
    terms = terms_for(cycle)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for proposal, revision in latest_rows(db, cycle.id):
        if proposal.state != "LOCKED":
            raise FinanceInputError("COMPENSATION_EXPORT_REQUIRES_LOCKED_AWARDS")
        frozen = frozen_for(db, cycle, proposal.employee_id)
        effective = next(
            (row for row in frozen.history if row.active_at(terms.merit_effective_date)), None
        )
        award = revision.award
        row: dict[str, Any] = {
            "employee_id": proposal.employee_id,
            "org_id": award["org_id"],
            "cost_center": pools[memberships[proposal.employee_id]]["cost_center"],
            "currency": award["currency"],
            "currency_exponent": EXPONENTS[award["currency"]],
            "merit_effective_date": terms.merit_effective_date.isoformat(),
            "base_pay_full_time_minor": award["effective_base_pay_minor"] or "",
            "merit_full_time_increment_minor": exact_decimal(revision.merit_minor),
            "new_base_pay_full_time_minor": award["resulting_base_pay_minor"] or "",
            "fte_at_merit_effective_date": effective.fte if effective else "0",
            "annualized_merit_cost_minor": award["annualized_merit_minor"],
            "first_year_merit_cost_minor": award["merit_first_year_minor"],
            "bonus_payment_date": terms.bonus_payment_date.isoformat(),
            "bonus_target_full_time_minor": exact_decimal(revision.bonus_minor),
            "bonus_payable_minor": award["bonus_payable_minor"],
            "pool_charge_minor": award["pool_charge_minor"],
            "proposal_revision": revision.number,
            "snapshot_id": cycle.snapshot_id,
            "guideline_id": cycle.guideline_id,
            "fx_set_id": cycle.fx_set_id,
        }
        writer.writerow({column: safe_cell(str(row[column])) for column in CSV_COLUMNS})
    data = stream.getvalue().encode()
    if len(data) > MAX_UPLOAD_BYTES:
        raise FinanceInputError("COMPENSATION_EXPORT_SIZE_LIMIT")
    return data


def csv_totals(data: bytes) -> tuple[int, dict[str, dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(data.decode()))
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise FinanceInputError("COMPENSATION_EXPORT_SCHEMA_MISMATCH")
    totals: dict[str, dict[str, int]] = {}
    people: set[str] = set()
    for row in reader:
        if row["currency"] not in EXPONENTS or row["employee_id"] in people or len(people) >= 5000:
            raise FinanceInputError("COMPENSATION_EXPORT_POPULATION_MISMATCH")
        people.add(row["employee_id"])
        values = totals.setdefault(row["currency"], dict.fromkeys(SUM_FIELDS.values(), 0))
        for column, field in SUM_FIELDS.items():
            value = row[column]
            if not value.isascii() or not value.isdigit():
                raise FinanceInputError("COMPENSATION_EXPORT_INVALID_MONEY")
            values[field] += int(value)
        if int(row["annualized_merit_cost_minor"]) + int(row["bonus_payable_minor"]) != int(
            row["pool_charge_minor"]
        ):
            raise FinanceInputError("COMPENSATION_EXPORT_ROW_SUM_MISMATCH")
    return len(people), {
        currency: {field: str(value) for field, value in values.items()}
        for currency, values in sorted(totals.items())
    }


def verified_artifact(db: Session, cycle: CompCycle) -> CompExportArtifact:
    """Mint no link until actual pinned files, counts and monetary sums reconcile."""
    artifact = db.get(CompExportArtifact, cycle.id)
    locked = db.get(CompLock, artifact.lock_id) if artifact else None
    if artifact is None or locked is None:
        raise DomainError(
            409, "comp_export_unavailable", "The counted export is not available yet."
        )
    if artifact.sha256 != canonical_hash(artifact.document) or not verify_manifest(
        artifact.document
    ):
        raise DomainError(
            503, "comp_export_verification_failed", "The pinned export could not be verified."
        )
    manifest = artifact.document["manifest"]
    try:
        data = read_private(artifact.csv_key)
        manifest_bytes = read_private(artifact.manifest_key)
    except OSError as error:
        raise DomainError(
            503,
            "comp_export_storage_unavailable",
            "The private export files are unavailable or corrupt.",
        ) from error
    count, totals = csv_totals(data)
    if (
        locked.sha256 != canonical_hash(locked.document)
        or manifest_bytes != canonical_bytes(artifact.document)
        or manifest["lock_id"] != locked.id
        or manifest["lock_sha256"] != locked.sha256
        or manifest["csv_sha256"] != artifact.csv_key
        or count != artifact.row_count
        or count != locked.document["row_count"]
        or totals != manifest["totals_by_currency"]
        or totals != locked.document["totals_by_currency"]
    ):
        raise DomainError(
            503,
            "comp_export_verification_failed",
            "The pinned export does not reconcile with its lock.",
        )
    return artifact


def write_export(
    db: Session, cycle_id: str, lock_id: str, source_batch_id: str
) -> CompExportArtifact:
    cycle = db.scalar(
        select(CompCycle)
        .where(CompCycle.id == cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = db.get(CompLock, lock_id)
    snapshot = db.get(Snapshot, cycle.snapshot_id) if cycle else None
    if (
        cycle is None
        or locked is None
        or snapshot is None
        or locked.cycle_id != cycle.id
        or snapshot.source_batch_id != source_batch_id
    ):
        raise FinanceInputError("COMPENSATION_EXPORT_EVENT_SOURCE_BINDING_MISMATCH")
    stored = db.get(CompExportArtifact, cycle.id)
    if stored is not None:
        if (
            stored.lock_id != locked.id
            or canonical_hash(stored.document) != stored.sha256
            or not verify_manifest(stored.document)
            or read_private(stored.csv_key) != csv_for(db, cycle, locked)
            or read_private(stored.manifest_key) != canonical_bytes(stored.document)
        ):
            raise FinanceInputError("COMPENSATION_EXISTING_EXPORT_FINGERPRINT_MISMATCH")
        return stored
    if cycle.state != "LOCKED":
        raise FinanceInputError("COMPENSATION_EXPORT_CYCLE_NOT_LOCKED")
    for proposal, revision in latest_rows(db, cycle.id):
        verify_revision(db, proposal.id, revision.id, cycle.id, source_batch_id)
    data = csv_for(db, cycle, locked)
    count, totals = csv_totals(data)
    if count != locked.document["row_count"] or totals != locked.document["totals_by_currency"]:
        raise FinanceInputError("COMPENSATION_EXPORT_COUNT_SUM_RECONCILIATION_FAILED")
    csv_key = put_private(data)
    decisions = locked.document["decisions"]
    manifest = {
        "schema_version": 1,
        "artifact_type": "compensation_review",
        "cycle_id": cycle.id,
        "version": 1,
        "lock_id": locked.id,
        "lock_sha256": locked.sha256,
        "inputs": locked.document["inputs"],
        "review_round_id": locked.round_id,
        "review_round_sha256": locked.document["review_round_sha256"],
        "row_count": count,
        "totals_by_currency": totals,
        "reporting_currency": locked.document["reporting_currency"],
        "reporting_pool_charge_minor": locked.document["reporting_pool_charge_minor"],
        "csv_sha256": csv_key,
        "csv_columns": list(CSV_COLUMNS),
        "decision_count": len(decisions),
        "decisions_sha256": canonical_hash(decisions),
        "decision_ids": [decision["id"] for decision in decisions],
        "proposal_revisions": locked.document["proposal_revisions"],
        "locked_at": locked.created_at.isoformat(),
        "destination": "private local artifact only; no payroll, HRIS or warehouse transmission",
    }
    signed = signed_manifest(manifest)
    manifest_bytes = canonical_bytes(signed)
    if len(manifest_bytes) > MAX_UPLOAD_BYTES:
        raise FinanceInputError("COMPENSATION_MANIFEST_SIZE_LIMIT")
    manifest_key = put_private(manifest_bytes)
    artifact = CompExportArtifact(
        cycle_id=cycle.id,
        lock_id=locked.id,
        version=1,
        csv_key=csv_key,
        manifest_key=manifest_key,
        sha256=canonical_hash(signed),
        row_count=count,
        document=signed,
    )
    db.add(artifact)
    db.flush()
    source_action = db.get(CompCycleAction, locked.action_id)
    if source_action is None:
        raise FinanceInputError("COMPENSATION_LOCK_ACTION_UNAVAILABLE")
    record_cycle_action(
        db,
        cycle,
        action="EXPORTED",
        actor_id=None,
        reason="Worker verified and wrote counted signed local artifact",
        evidence=artifact.sha256,
        request_id=source_action.request_id,
        state="EXPORTED",
    )
    record(
        db,
        None,
        "comp.export_created",
        "comp_cycle",
        cycle.id,
        source_action.request_id,
        after_hash=artifact.sha256,
        entity_version=cycle.version,
        event_payload={
            "entity_type": "comp_cycle",
            "entity_id": cycle.id,
            "lock_id": locked.id,
            "source_batch_id": source_batch_id,
        },
    )
    return artifact


def mark_failed_export(
    db: Session, cycle_id: str, lock_id: str, source_batch_id: str, request_id: str
) -> None:
    cycle = db.scalar(select(CompCycle).where(CompCycle.id == cycle_id).with_for_update())
    locked = db.get(CompLock, lock_id)
    snapshot = db.get(Snapshot, cycle.snapshot_id) if cycle else None
    if (
        cycle is not None
        and locked is not None
        and snapshot is not None
        and locked.cycle_id == cycle.id
        and snapshot.source_batch_id == source_batch_id
        and cycle.state == "LOCKED"
    ):
        record_cycle_action(
            db,
            cycle,
            action="FAILED_EXPORT",
            actor_id=None,
            reason="Export job exhausted bounded retries; inspect private storage availability",
            evidence=locked.sha256,
            request_id=request_id,
            state="FAILED_EXPORT",
        )


def reconcile_export(
    db: Session, cycle: CompCycle, *, reason: str, request_id: str
) -> CompReconciliation:
    if cycle.state != "EXPORTED":
        raise DomainError(
            409, "comp_reconciliation_unavailable", "Reconciliation requires a completed export."
        )
    artifact = db.get(CompExportArtifact, cycle.id)
    if artifact is None:
        raise FinanceInputError("COMPENSATION_EXPORT_UNAVAILABLE")
    locked = db.get(CompLock, artifact.lock_id)
    if (
        locked is None
        or artifact.sha256 != canonical_hash(artifact.document)
        or not verify_manifest(artifact.document)
    ):
        raise FinanceInputError("COMPENSATION_EXPORT_SIGNATURE_OR_FINGERPRINT_MISMATCH")
    actual = read_private(artifact.csv_key)
    expected = csv_for(db, cycle, locked)
    count, totals = csv_totals(actual)
    manifest_bytes = read_private(artifact.manifest_key)
    if (
        actual != expected
        or manifest_bytes != canonical_bytes(artifact.document)
        or count != artifact.row_count
        or totals != locked.document["totals_by_currency"]
    ):
        raise FinanceInputError("COMPENSATION_EXPORT_SOURCE_RECONCILIATION_FAILED")
    document = {
        "result": "MATCH",
        "adapter": "local immutable CSV versus locked PostgreSQL revisions",
        "warehouse_validation": "UNVERIFIED; this source comparison does not execute analytics or Snowflake",
        "row_count": count,
        "totals_by_currency": totals,
        "csv_sha256": artifact.csv_key,
        "manifest_sha256": artifact.sha256,
        "lock_sha256": locked.sha256,
        "signature_verified": True,
        "byte_for_byte_source_match": True,
    }
    reconciled = CompReconciliation(
        cycle_id=cycle.id, actor_id=cycle.owner_id, export_sha256=artifact.sha256, document=document
    )
    db.add(reconciled)
    db.flush()
    record_cycle_action(
        db,
        cycle,
        action="RECONCILE",
        actor_id=cycle.owner_id,
        reason=reason,
        evidence=artifact.sha256,
        request_id=request_id,
        state="RECONCILED",
    )
    return reconciled
