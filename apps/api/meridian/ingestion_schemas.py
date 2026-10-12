from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from meridian.schemas import StrictModel


class UploadRequest(StrictModel):
    media_type: Literal["text/csv", "application/json"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0, le=8 * 1024 * 1024)


class UploadView(StrictModel):
    id: str
    upload_url: str
    expires_at: datetime
    max_bytes: int


class UploadAck(StrictModel):
    id: str
    sha256: str
    size_bytes: int


class BatchRequest(StrictModel):
    upload_id: UUID
    base_snapshot_id: UUID
    supersedes_batch_id: UUID | None = None
    title: str = Field(min_length=3, max_length=100)
    watermark: date
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def explicit_integer_schema(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value


class BatchView(StrictModel):
    id: str
    title: str
    state: str
    version: int
    accepted_count: int
    rejected_count: int
    total_count: int | None
    base_snapshot_id: str
    snapshot_id: str | None
    supersedes_batch_id: str | None
    source_sha256: str
    source_watermark: date
    received_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    parser_version: str
    pending_job: bool
    failed_job: bool
    manifest_ready: bool


class BatchList(StrictModel):
    items: list[BatchView]
    next_cursor: str | None


class BatchQuery(StrictModel):
    cursor: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ErrorView(StrictModel):
    row_number: int
    field: str
    code: str
    summary: str


class ErrorPage(StrictModel):
    items: list[ErrorView]
    next_cursor: int | None
    total: int


class ErrorQuery(StrictModel):
    cursor: int = Field(default=-1, ge=-1)
    code: str | None = Field(default=None, pattern=r"^[A-Z_]{1,40}$")
    limit: int = Field(default=50, ge=1, le=100)


class DownloadLink(StrictModel):
    url: str
    expires_at: datetime


class PublishRequest(StrictModel):
    confirm_zero_rejects: Literal[True]


class RetryRequest(StrictModel):
    reason: str = Field(min_length=8, max_length=200)


class PreviewRow(StrictModel):
    row_number: int
    employee_id: str
    display_name: str
    org_id: str
    manager_id: str | None
    valid_from: date
    valid_to: date | None
    status: str


class PreviewPage(StrictModel):
    items: list[PreviewRow]
    next_cursor: int | None
    total: int


class PreviewQuery(StrictModel):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=100)


class LinkRequest(StrictModel):
    kind: Literal["errors", "manifest"]
