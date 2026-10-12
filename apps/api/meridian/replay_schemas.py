"""Explicit full and redacted response fields; no generic private capsule JSON."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from meridian.compensation_schemas import AwardView
from meridian.scenario_operations import ScenarioOperation
from meridian.scenario_schemas import MoneyComponents, ReasonRequest
from meridian.schemas import StrictModel

Kind = Literal["SCENARIO", "AWARD"]


class ReplayReason(ReasonRequest):
    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("A meaningful reason is required")
        return value


class ReplayRequest(ReplayReason):
    kind: Kind
    subject_id: UUID


class ReplayMoneyMonth(StrictModel):
    month: str
    planned: MoneyComponents
    baseline: MoneyComponents
    delta_minor: str


class ReplayAggregate(StrictModel):
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    exponent: Literal[0, 2, 3]
    planned: MoneyComponents
    baseline: MoneyComponents
    delta_minor: str
    months: list[ReplayMoneyMonth] = Field(min_length=12, max_length=12)


class ReplayOperation(StrictModel):
    id: str
    kind: str
    effective_date: date
    sha256: str


class ReplayDecision(StrictModel):
    id: str
    revision_id: str
    action: str
    actor_reference: str
    ordinal: int
    correlation_id: str
    at: datetime
    reason_sha256: str
    reason: str | None = None
    role: str | None = None
    exceptions: list[str] = []


class ReplayTimeline(StrictModel):
    id: str
    kind: Literal["AUDIT", "OUTBOX"]
    verb: str
    at: datetime
    correlation_id: str
    sha256: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    policy_version: str | None = None
    actor_reference: str | None = None


class ReplayLineage(StrictModel):
    batch_id: str
    sha256: str
    source_type: str
    schema_version: int
    watermark: date
    accepted_count: int
    rejected_count: int
    supersedes_batch_id: str | None
    base_snapshot_id: str | None
    published_at: datetime | None


class ReplayPin(StrictModel):
    name: str
    version: str
    sha256: str | None = None


class ReplayAttemptView(StrictModel):
    id: str
    number: int
    status: Literal["MATCH", "MISSING", "CORRUPT", "UNSUPPORTED"]
    expected_sha256: str
    observed_sha256: str | None
    issues: list[str]
    request_id: str
    created_at: datetime
    export_ready: bool
    artifact_issues: list[str] = []


class ReplayAwardRevision(StrictModel):
    revision_id: str
    number: int
    award: AwardView


class ReplayView(StrictModel):
    id: str
    kind: Kind
    subject_id: str
    snapshot_id: str
    snapshot_sha256: str
    source_watermark: date
    created_at: datetime
    sha256: str
    redacted: bool
    aggregate: ReplayAggregate | None
    award: AwardView | None
    full_operations: list[ScenarioOperation]
    operations: list[ReplayOperation]
    decisions: list[ReplayDecision]
    timeline: list[ReplayTimeline]
    source_lineage: list[ReplayLineage]
    pins: list[ReplayPin]
    attempts: list[ReplayAttemptView]
    integrity_issues: list[str]
    # Safe version/reference-only hierarchy; employee IDs and narrative text never appear here.
    hierarchy: list[dict[str, str]]
    approval_steps: list[dict[str, str | int | list[str]]]
    revision_history: list[dict[str, str | int]]
    award_history: list[ReplayAwardRevision]


class ReplayChoice(StrictModel):
    kind: Kind
    subject_id: str
    label: str


class ReplaySummary(StrictModel):
    id: str
    kind: Kind
    subject_id: str
    sha256: str
    created_at: datetime


class ReplayList(StrictModel):
    items: list[ReplaySummary]
    next_cursor: str | None


class AuditQuery(StrictModel):
    cursor: UUID | None = None
    limit: int = Field(default=25, ge=1, le=50)
    correlation_id: UUID | None = None
    permission: Literal["ALLOW", "DENY"] | None = None
    verb: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9_.]+$")


class AuditItem(StrictModel):
    id: str
    verb: str
    entity_type: str
    entity_reference: str
    actor_reference: str | None
    request_id: str
    permission: Literal["ALLOW", "DENY"]
    policy_version: str
    occurred_at: datetime
    before_sha256: str | None
    after_sha256: str | None
    sha256: str


class AuditPage(StrictModel):
    items: list[AuditItem]
    next_cursor: str | None
