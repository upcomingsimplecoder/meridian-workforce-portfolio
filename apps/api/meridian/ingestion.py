import csv
import hashlib
import io
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from meridian import object_store
from meridian.errors import DomainError
from meridian.governance import record
from meridian.idempotency import lock_key, remember, replay
from meridian.identity import DB, Actor, CurrentActor, require
from meridian.ingestion_models import (
    IngestionError,
    IngestionInput,
    IngestionManifest,
    IngestionRecord,
    UploadIntent,
)
from meridian.ingestion_parser import synthetic_public_fields
from meridian.ingestion_schemas import (
    BatchList,
    BatchQuery,
    BatchRequest,
    BatchView,
    DownloadLink,
    ErrorPage,
    ErrorQuery,
    ErrorView,
    LinkRequest,
    PreviewPage,
    PreviewQuery,
    PreviewRow,
    PublishRequest,
    RetryRequest,
    UploadAck,
    UploadRequest,
    UploadView,
)
from meridian.ingestion_service import publish_batch
from meridian.models import OutboxEvent, now
from meridian.people import select_snapshot
from meridian.people_models import Snapshot, SourceBatch
from meridian.policy import canonical_hash

router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])
IdempotencyKey = Annotated[UUID, Header(alias="Idempotency-Key")]
IfMatch = Annotated[int, Header(alias="If-Match", ge=1)]


def require_ingestion(current: Actor, db: Session, request: Request) -> None:
    require(current, "ingestion.write", db, request)
    if not any(
        grant.scope == "*" and "ingestion.write" in current.policy.get(grant.role, [])
        for grant in current.grants
    ):
        record(
            db,
            current.principal.id,
            "security.ingestion_scope_denied",
            "route",
            request.url.path,
            request.state.request_id,
            False,
        )
        db.commit()
        raise DomainError(
            403, "forbidden", "Ingestion requires an organization-wide steward grant."
        )


def can_manage_all(current: Actor) -> bool:
    return any(grant.role == "hr" and grant.scope == "*" for grant in current.grants)


def batch_for(
    db: Session, current: Actor, request: Request, batch_id: str, lock: bool = False
) -> tuple[SourceBatch, IngestionInput]:
    require_ingestion(current, db, request)
    query = (
        select(SourceBatch)
        .join(IngestionInput, IngestionInput.batch_id == SourceBatch.id)
        .where(SourceBatch.id == batch_id)
    )
    if not can_manage_all(current):
        query = query.where(SourceBatch.actor_id == current.principal.id)
    if lock:
        query = query.with_for_update(of=SourceBatch)
    batch = db.scalar(query)
    source = db.get(IngestionInput, batch.id) if batch else None
    if batch is None or source is None:
        record(
            db,
            current.principal.id,
            "security.batch_denied",
            "source_batch",
            batch_id,
            request.state.request_id,
            False,
        )
        db.commit()
        raise DomainError(404, "batch_unavailable", "Batch is unavailable in your permitted scope.")
    return batch, source


def batch_view(db: Session, batch: SourceBatch, source: IngestionInput) -> BatchView:
    jobs = list(
        db.scalars(
            select(OutboxEvent.state).where(
                OutboxEvent.aggregate_id == batch.id,
                OutboxEvent.event_type.in_(
                    ["source_batch.received.v1", "source_batch.published.v1"]
                ),
            )
        )
    )
    return BatchView(
        id=batch.id,
        title=source.title,
        state=batch.state,
        version=source.version,
        accepted_count=batch.accepted_count,
        rejected_count=batch.rejected_count,
        total_count=source.total_count,
        base_snapshot_id=source.base_snapshot_id,
        snapshot_id=db.scalar(select(Snapshot.id).where(Snapshot.source_batch_id == batch.id)),
        supersedes_batch_id=source.supersedes_batch_id,
        source_sha256=batch.sha256,
        source_watermark=batch.watermark,
        received_at=batch.received_at,
        validated_at=source.validated_at,
        published_at=batch.published_at,
        parser_version=source.parser_version,
        pending_job="PENDING" in jobs,
        failed_job="DEAD" in jobs,
        manifest_ready=db.get(IngestionManifest, batch.id) is not None,
    )


def persist_response(
    db: Session, current: Actor, request: Request, key: UUID, payload: Any, response: dict[str, Any]
) -> None:
    remember(
        db,
        current.principal.id,
        request.url.path,
        str(key),
        payload,
        response,
        request.state.request_id,
    )
    db.commit()


@router.post("/uploads", response_model=UploadView, status_code=201)
def create_upload(
    body: UploadRequest, current: CurrentActor, db: DB, request: Request, key: IdempotencyKey
) -> UploadView:
    require_ingestion(current, db, request)
    payload = body.model_dump(mode="json")
    previous = replay(db, current.principal.id, request.url.path, str(key), payload)
    if previous is not None:
        return UploadView.model_validate(previous)
    intent = UploadIntent(
        actor_id=current.principal.id,
        media_type=body.media_type,
        declared_sha256=body.sha256,
        declared_bytes=body.size_bytes,
        expires_at=now() + timedelta(minutes=5),
    )
    db.add(intent)
    db.flush()
    token = object_store.sign_link(current.principal.id, "upload", intent.id, 1)
    result = UploadView(
        id=intent.id,
        upload_url=f"/api/v1/ingestion/uploads/{intent.id}/content?token={token}",
        expires_at=intent.expires_at,
        max_bytes=body.size_bytes,
    )
    record(
        db,
        current.principal.id,
        "source_upload.created",
        "upload_intent",
        intent.id,
        request.state.request_id,
        emit=False,
    )
    persist_response(db, current, request, key, payload, result.model_dump(mode="json"))
    return result


@router.put(
    "/uploads/{upload_id}/content",
    response_model=UploadAck,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                media: {"schema": {"type": "string", "format": "binary"}}
                for media in ["text/csv", "application/json"]
            },
        }
    },
)
async def upload_content(
    upload_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    token: str = Query(max_length=1500),
) -> UploadAck:
    require_ingestion(current, db, request)
    intent = db.scalar(
        select(UploadIntent)
        .where(UploadIntent.id == str(upload_id), UploadIntent.actor_id == current.principal.id)
        .with_for_update()
    )
    if intent is None:
        raise DomainError(404, "upload_unavailable", "Upload is unavailable to this principal.")
    object_store.verify_link(token, current.principal.id, "upload", intent.id, 1)
    if intent.expires_at <= now():
        raise DomainError(403, "upload_expired", "Create a new upload intent.")
    if request.headers.get("content-type", "").split(";")[0].strip() != intent.media_type:
        raise DomainError(
            415, "content_type_mismatch", "Content type must match the signed upload intent."
        )
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > min(intent.declared_bytes, object_store.MAX_UPLOAD_BYTES):
            raise DomainError(413, "upload_limit", "Upload exceeded its declared size limit.")
        content.extend(chunk)
    data = bytes(content)
    if (
        len(data) != intent.declared_bytes
        or hashlib.sha256(data).hexdigest() != intent.declared_sha256
    ):
        raise DomainError(
            422,
            "object_checksum_mismatch",
            "Uploaded bytes do not match the declared size and SHA-256.",
        )
    key = object_store.put_private(data)
    if intent.storage_key is None:
        intent.storage_key = key
        intent.uploaded_at = now()
        record(
            db,
            current.principal.id,
            "source_upload.stored",
            "upload_intent",
            intent.id,
            request.state.request_id,
            after_hash=key,
            emit=False,
        )
    db.commit()
    return UploadAck(id=intent.id, sha256=key, size_bytes=len(data))


@router.post("/batches", response_model=BatchView, status_code=201)
def register_batch(
    body: BatchRequest, current: CurrentActor, db: DB, request: Request, key: IdempotencyKey
) -> BatchView:
    require_ingestion(current, db, request)
    payload = body.model_dump(mode="json")
    previous = replay(db, current.principal.id, request.url.path, str(key), payload)
    if previous is not None:
        batch, source = batch_for(db, current, request, previous["id"])
        return batch_view(db, batch, source)
    upload = db.scalar(
        select(UploadIntent).where(
            UploadIntent.id == str(body.upload_id), UploadIntent.actor_id == current.principal.id
        )
    )
    if upload is None or upload.storage_key is None:
        raise DomainError(
            404, "upload_unavailable", "Upload the verified object before registering it."
        )
    base = select_snapshot(db, str(body.base_snapshot_id))
    if body.supersedes_batch_id:
        old, _ = batch_for(db, current, request, str(body.supersedes_batch_id))
        if old.state != "QUARANTINED":
            raise DomainError(
                409, "invalid_correction", "A correction must refer to a quarantined batch."
            )
    lock_key(db, "source:" + upload.declared_sha256)
    duplicate = db.scalar(
        select(SourceBatch).where(
            SourceBatch.source_type == "WORKFORCE_V1", SourceBatch.sha256 == upload.declared_sha256
        )
    )
    if duplicate is not None:
        batch, source = batch_for(db, current, request, duplicate.id)
        if source.base_snapshot_id != base.id or batch.watermark != body.watermark:
            raise DomainError(
                409,
                "duplicate_context_conflict",
                "Identical source bytes were already registered with a different base or watermark.",
            )
    else:
        batch = SourceBatch(
            source_type="WORKFORCE_V1",
            sha256=upload.declared_sha256,
            state="RECEIVED",
            accepted_count=0,
            rejected_count=0,
            watermark=body.watermark,
            actor_id=current.principal.id,
        )
        db.add(batch)
        db.flush()
        source = IngestionInput(
            batch_id=batch.id,
            upload_id=upload.id,
            base_snapshot_id=base.id,
            title=body.title,
            supersedes_batch_id=str(body.supersedes_batch_id) if body.supersedes_batch_id else None,
        )
        db.add(source)
        db.flush()
        record(
            db,
            current.principal.id,
            "source_batch.received",
            "source_batch",
            batch.id,
            request.state.request_id,
            after_hash=batch.sha256,
        )
        db.flush()
    result = batch_view(db, batch, source)
    persist_response(db, current, request, key, payload, result.model_dump(mode="json"))
    return result


@router.get("/batches", response_model=BatchList)
def batches(
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[BatchQuery, Query()],
) -> BatchList:
    require_ingestion(current, db, request)
    query = select(SourceBatch).join(IngestionInput, IngestionInput.batch_id == SourceBatch.id)
    if not can_manage_all(current):
        query = query.where(SourceBatch.actor_id == current.principal.id)
    if filters.cursor:
        anchor = db.scalar(query.where(SourceBatch.id == str(filters.cursor)))
        if anchor is None:
            raise DomainError(422, "invalid_cursor", "Source cursor is unavailable in this scope.")
        query = query.where(
            tuple_(SourceBatch.received_at, SourceBatch.id) < (anchor.received_at, anchor.id)
        )
    limit = filters.limit
    rows = list(
        db.scalars(
            query.order_by(SourceBatch.received_at.desc(), SourceBatch.id.desc()).limit(limit + 1)
        )
    )
    items = []
    for row in rows[:limit]:
        source = db.get(IngestionInput, row.id)
        if source is not None:
            items.append(batch_view(db, row, source))
    return BatchList(items=items, next_cursor=rows[limit - 1].id if len(rows) > limit else None)


@router.get("/batches/{batch_id}", response_model=BatchView)
def batch_detail(batch_id: UUID, current: CurrentActor, db: DB, request: Request) -> BatchView:
    batch, source = batch_for(db, current, request, str(batch_id))
    return batch_view(db, batch, source)


@router.get("/batches/{batch_id}/errors", response_model=ErrorPage)
def batch_errors(
    batch_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[ErrorQuery, Query()],
) -> ErrorPage:
    batch_for(db, current, request, str(batch_id))
    query = select(IngestionError).where(IngestionError.batch_id == str(batch_id))
    if filters.code:
        query = query.where(IngestionError.code == filters.code)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        db.scalars(
            query.where(IngestionError.row_number > filters.cursor)
            .order_by(IngestionError.row_number)
            .limit(filters.limit + 1)
        )
    )
    return ErrorPage(
        items=[
            ErrorView(
                row_number=row.row_number,
                field=row.field,
                code=row.code,
                summary=row.redacted_summary,
            )
            for row in rows[: filters.limit]
        ],
        total=total,
        next_cursor=rows[filters.limit - 1].row_number if len(rows) > filters.limit else None,
    )


@router.get("/batches/{batch_id}/preview", response_model=PreviewPage)
def batch_preview(
    batch_id: UUID,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: Annotated[PreviewQuery, Query()],
) -> PreviewPage:
    batch, _ = batch_for(db, current, request, str(batch_id))
    limit = filters.limit
    rows = list(
        db.scalars(
            select(IngestionRecord)
            .where(
                IngestionRecord.batch_id == batch.id, IngestionRecord.row_number > filters.cursor
            )
            .order_by(IngestionRecord.row_number)
            .limit(limit + 1)
        )
    )
    return PreviewPage(
        items=[
            PreviewRow.model_validate(
                {"row_number": row.row_number} | synthetic_public_fields(row.canonical)
            )
            for row in rows[:limit]
        ],
        total=batch.accepted_count,
        next_cursor=rows[limit - 1].row_number if len(rows) > limit else None,
    )


@router.post("/batches/{batch_id}/publish", response_model=BatchView)
def publish(
    batch_id: UUID,
    body: PublishRequest,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: IdempotencyKey,
    version: IfMatch,
) -> BatchView:
    batch, source = batch_for(db, current, request, str(batch_id), lock=True)
    payload = body.model_dump(mode="json") | {"version": version}
    previous = replay(db, current.principal.id, request.url.path, str(key), payload)
    if previous is not None:
        return batch_view(db, batch, source)
    if source.version != version:
        raise DomainError(
            409,
            "version_conflict",
            f"Batch revision changed; reload revision {source.version} before publishing.",
        )
    publish_batch(db, batch, source, current.principal.id, request.state.request_id)
    db.flush()
    result = batch_view(db, batch, source)
    persist_response(db, current, request, key, payload, result.model_dump(mode="json"))
    return result


@router.post("/batches/{batch_id}/retry", response_model=BatchView)
def retry(
    batch_id: UUID,
    body: RetryRequest,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: IdempotencyKey,
    version: IfMatch,
) -> BatchView:
    batch, source = batch_for(db, current, request, str(batch_id), lock=True)
    payload = body.model_dump(mode="json") | {"version": version}
    if replay(db, current.principal.id, request.url.path, str(key), payload) is not None:
        return batch_view(db, batch, source)
    if version != source.version:
        raise DomainError(
            409, "version_conflict", "Batch revision changed; reload before retrying."
        )
    jobs = list(
        db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.aggregate_id == batch.id, OutboxEvent.state == "DEAD")
            .with_for_update()
        )
    )
    if not jobs:
        raise DomainError(
            409,
            "nothing_to_retry",
            "No failed job exists. Pending jobs resume automatically with the worker.",
        )
    for job in jobs:
        job.state = "PENDING"
        job.attempts = 0
        job.next_attempt_at = now()
        job.last_error = None
    source.version += 1
    record(
        db,
        current.principal.id,
        "source_batch.retried",
        "source_batch",
        batch.id,
        request.state.request_id,
        after_hash=canonical_hash(payload),
        emit=False,
        entity_version=source.version,
    )
    db.flush()
    result = batch_view(db, batch, source)
    persist_response(db, current, request, key, payload, result.model_dump(mode="json"))
    return result


@router.post("/batches/{batch_id}/download-links", response_model=DownloadLink)
def download_link(
    batch_id: UUID,
    body: LinkRequest,
    current: CurrentActor,
    db: DB,
    request: Request,
    key: IdempotencyKey,
) -> DownloadLink:
    batch, source = batch_for(db, current, request, str(batch_id))
    payload = body.model_dump(mode="json") | {"version": source.version}
    previous = replay(db, current.principal.id, request.url.path, str(key), payload)
    if previous is not None:
        return DownloadLink.model_validate(previous)
    if body.kind == "manifest" and db.get(IngestionManifest, batch.id) is None:
        raise DomainError(409, "artifact_pending", "The worker has not produced this manifest yet.")
    expires = now() + timedelta(seconds=60)
    token = object_store.sign_link(current.principal.id, body.kind, batch.id, source.version, 60)
    result = DownloadLink(
        url=f"/api/v1/ingestion/batches/{batch.id}/downloads/{body.kind}?token={token}",
        expires_at=expires,
    )
    record(
        db,
        current.principal.id,
        "source_batch.download_authorized",
        "source_batch",
        batch.id,
        request.state.request_id,
        emit=False,
        entity_version=source.version,
    )
    persist_response(db, current, request, key, payload, result.model_dump(mode="json"))
    return result


@router.get("/batches/{batch_id}/downloads/{kind}", response_class=Response)
def download(
    batch_id: UUID,
    kind: str,
    current: CurrentActor,
    db: DB,
    request: Request,
    token: str = Query(max_length=1500),
) -> Response:
    batch, source = batch_for(db, current, request, str(batch_id))
    object_store.verify_link(token, current.principal.id, kind, batch.id, source.version)
    if kind == "errors":
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["row_number", "field", "code", "redacted_summary"])
        for error in db.scalars(
            select(IngestionError)
            .where(IngestionError.batch_id == batch.id)
            .order_by(IngestionError.row_number)
        ):
            writer.writerow([error.row_number, error.field, error.code, error.redacted_summary])
        data = output.getvalue().encode()
        media = "text/csv"
        name = "meridian-errors.csv"
    elif kind == "manifest":
        manifest = db.get(IngestionManifest, batch.id)
        if manifest is None:
            raise DomainError(409, "artifact_pending", "Manifest is unavailable.")
        data = object_store.read_private(manifest.storage_key)
        media = "application/json"
        name = "meridian-publication-manifest.json"
    else:
        raise DomainError(404, "artifact_unavailable", "Unknown artifact type.")
    record(
        db,
        current.principal.id,
        "source_batch.downloaded",
        "source_batch",
        batch.id,
        request.state.request_id,
        emit=False,
        entity_version=source.version,
    )
    db.commit()
    return Response(
        data,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
