import json
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from meridian import object_store
from meridian.errors import DomainError
from meridian.governance import record
from meridian.ingestion_models import (
    IngestionError,
    IngestionInput,
    IngestionManifest,
    IngestionRecord,
    SnapshotPublication,
    UploadIntent,
)
from meridian.ingestion_parser import FIELDS, merge_histories, parse_source
from meridian.models import now, uid
from meridian.people_models import (
    Employee,
    EmployeeVersion,
    OrgVersion,
    Position,
    Snapshot,
    SourceBatch,
)
from meridian.people_seed import stable_id
from meridian.policy import canonical_hash


def base_rows(db: Session, snapshot_id: str) -> list[dict[str, Any]]:
    rows = []
    for row in db.scalars(
        select(EmployeeVersion)
        .where(EmployeeVersion.snapshot_id == snapshot_id)
        .order_by(EmployeeVersion.employee_id, EmployeeVersion.valid_from)
    ):
        item = {field: getattr(row, field) for field in FIELDS}
        item.update(
            annual_pay_minor=format(row.annual_pay_minor, ".0f"),
            fte=format(row.fte, ".4f"),
            valid_from=row.valid_from.isoformat(),
            valid_to=row.valid_to.isoformat() if row.valid_to else None,
            source_batch_id=row.source_batch_id,
            recorded_at=row.recorded_at.isoformat(),
        )
        rows.append(item)
    return rows


def validate_batch(db: Session, batch_id: str) -> None:
    batch = db.scalar(select(SourceBatch).where(SourceBatch.id == batch_id).with_for_update())
    source = db.get(IngestionInput, batch_id)
    if batch is None or source is None:
        raise ValueError("Unrecognized ingestion batch")
    if batch.state != "RECEIVED":
        return
    upload = db.get(UploadIntent, source.upload_id)
    if upload is None or upload.storage_key is None:
        raise OSError("Source object unavailable")
    contents = object_store.read_private(upload.storage_key)
    org_intervals: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for org in db.scalars(
        select(OrgVersion).where(OrgVersion.snapshot_id == source.base_snapshot_id)
    ):
        org_intervals[org.org_id].append(
            (org.valid_from.isoformat(), org.valid_to.isoformat() if org.valid_to else None)
        )
    parsed = parse_source(
        contents,
        upload.media_type,
        batch.watermark,
        base_rows(db, source.base_snapshot_id),
        set(org_intervals),
        dict(org_intervals),
    )
    if parsed.records:
        db.execute(
            insert(IngestionRecord),
            [
                {"batch_id": batch_id, "row_number": number, "canonical": row}
                for number, row in parsed.records
            ],
        )
    if parsed.errors:
        db.execute(
            insert(IngestionError),
            [
                {
                    "batch_id": batch_id,
                    "row_number": error.row_number,
                    "field": error.field,
                    "code": error.code,
                    "redacted_summary": error.summary,
                }
                for error in parsed.errors
            ],
        )
    # Flush inserts while the parent remains open, then seal the validation outcome.
    db.flush()
    batch.accepted_count = len(parsed.records)
    batch.rejected_count = parsed.total - len(parsed.records)
    batch.state = "QUARANTINED" if parsed.errors else "VALIDATED"
    source.total_count = parsed.total
    source.validated_at = now()
    source.version += 1
    record(
        db,
        None,
        "source_batch.validated",
        "source_batch",
        batch.id,
        uid(),
        after_hash=canonical_hash(
            {
                "accepted": batch.accepted_count,
                "rejected": batch.rejected_count,
                "parser": source.parser_version,
            }
        ),
        emit=False,
        entity_version=source.version,
    )


def publish_batch(
    db: Session, batch: SourceBatch, source: IngestionInput, actor: str, request_id: str
) -> Snapshot:
    if batch.state != "VALIDATED" or batch.rejected_count or not batch.accepted_count:
        raise DomainError(
            409,
            "batch_not_publishable",
            "Publish requires a completed validation with zero rejects.",
        )
    if db.scalar(
        select(func.count()).select_from(IngestionError).where(IngestionError.batch_id == batch.id)
    ):
        raise DomainError(
            409, "batch_not_publishable", "Resolve every batch validation error before publication."
        )
    published_at = now()
    incoming = [
        dict(row.canonical) | {"source_batch_id": batch.id, "recorded_at": published_at.isoformat()}
        for row in db.scalars(
            select(IngestionRecord)
            .where(IngestionRecord.batch_id == batch.id)
            .order_by(IngestionRecord.row_number)
        )
    ]
    merged = merge_histories(base_rows(db, source.base_snapshot_id), incoming)
    snapshot = Snapshot(
        source_batch_id=batch.id,
        title=source.title,
        source_watermark=batch.watermark,
        captured_at=published_at,
        sha256=canonical_hash(merged),
        row_count=len({row["employee_id"] for row in merged}),
        status="BUILDING",
    )
    db.add(snapshot)
    db.flush()
    db.execute(
        pg_insert(Employee)
        .values([{"id": person} for person in sorted({row["employee_id"] for row in merged})])
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.execute(
        pg_insert(Position)
        .values([{"id": position} for position in sorted({row["position_id"] for row in merged})])
        .on_conflict_do_nothing(index_elements=["id"])
    )
    # Each copied version retains its source provenance even when its interval is split.
    db.execute(
        insert(EmployeeVersion),
        [
            row
            | {
                "id": stable_id(f"{snapshot.id}:{row['employee_id']}:{row['valid_from']}"),
                "snapshot_id": snapshot.id,
                "valid_from": date.fromisoformat(row["valid_from"]),
                "valid_to": date.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
                "annual_pay_minor": Decimal(row["annual_pay_minor"]),
                "fte": Decimal(row["fte"]),
                "recorded_at": datetime.fromisoformat(row["recorded_at"]),
            }
            for row in merged
        ],
    )
    db.add_all(
        [
            OrgVersion(
                snapshot_id=snapshot.id,
                org_id=row.org_id,
                parent_id=row.parent_id,
                name=row.name,
                region=row.region,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
            )
            for row in db.scalars(
                select(OrgVersion).where(OrgVersion.snapshot_id == source.base_snapshot_id)
            )
        ]
    )
    db.flush()
    snapshot.status = "PUBLISHED"
    db.add(SnapshotPublication(snapshot_id=snapshot.id))
    batch.state = "PUBLISHED"
    batch.published_at = published_at
    source.version += 1
    record(
        db,
        actor,
        "source_batch.published",
        "source_batch",
        batch.id,
        request_id,
        before_hash=batch.sha256,
        after_hash=snapshot.sha256,
        entity_version=source.version,
    )
    return snapshot


def write_manifest(db: Session, batch_id: str) -> None:
    if db.get(IngestionManifest, batch_id):
        return
    batch = db.get(SourceBatch, batch_id)
    source = db.get(IngestionInput, batch_id)
    snapshot = db.scalar(
        select(Snapshot).where(Snapshot.source_batch_id == batch_id, Snapshot.status == "PUBLISHED")
    )
    if batch is None or source is None or snapshot is None:
        raise ValueError("Published source is unavailable")
    document = {
        "schema_version": 1,
        "batch_id": batch.id,
        "snapshot_id": snapshot.id,
        "source_sha256": batch.sha256,
        "snapshot_sha256": snapshot.sha256,
        "source_watermark": batch.watermark.isoformat(),
        "published_at": batch.published_at.isoformat() if batch.published_at else None,
        "accepted": batch.accepted_count,
        "rejected": batch.rejected_count,
        "total": source.total_count,
        "person_identities": snapshot.row_count,
        "employee_versions": db.scalar(
            select(func.count())
            .select_from(EmployeeVersion)
            .where(EmployeeVersion.snapshot_id == snapshot.id)
        ),
        "parser_version": source.parser_version,
        "base_snapshot_id": source.base_snapshot_id,
        "supersedes_batch_id": source.supersedes_batch_id,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    key = object_store.put_private(encoded)
    db.add(
        IngestionManifest(
            batch_id=batch.id,
            snapshot_id=snapshot.id,
            sha256=key,
            storage_key=key,
            document=document,
        )
    )
