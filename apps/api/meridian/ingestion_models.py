from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db import Base
from meridian.models import now, uid


class UploadIntent(Base):
    __tablename__ = "upload_intent"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    media_type: Mapped[str] = mapped_column(String(30))
    declared_sha256: Mapped[str] = mapped_column(String(64))
    declared_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionInput(Base):
    __tablename__ = "ingestion_input"
    batch_id: Mapped[str] = mapped_column(ForeignKey("source_batch.id"), primary_key=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("upload_intent.id"), unique=True)
    base_snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), index=True)
    supersedes_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_batch.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    total_count: Mapped[int | None] = mapped_column(Integer)
    parser_version: Mapped[str] = mapped_column(String(30), default="workforce-source-v1")
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionRecord(Base):
    __tablename__ = "ingestion_record"
    batch_id: Mapped[str] = mapped_column(ForeignKey("source_batch.id"), primary_key=True)
    row_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical: Mapped[dict[str, Any]] = mapped_column(JSONB)


class IngestionError(Base):
    __tablename__ = "ingestion_error"
    __table_args__ = (UniqueConstraint("batch_id", "row_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("source_batch.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(40))
    code: Mapped[str] = mapped_column(String(40))
    redacted_summary: Mapped[str] = mapped_column(String(150))


class SnapshotPublication(Base):
    __tablename__ = "snapshot_publication"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)


class IdempotencyReceipt(Base):
    __tablename__ = "idempotency_receipt"
    __table_args__ = (UniqueConstraint("actor_id", "route", "key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principal.id"), index=True)
    route: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(36))
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    request_sha256: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IngestionManifest(Base):
    __tablename__ = "ingestion_manifest"
    batch_id: Mapped[str] = mapped_column(ForeignKey("source_batch.id"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
