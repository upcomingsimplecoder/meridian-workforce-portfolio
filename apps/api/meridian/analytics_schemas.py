"""Allowlisted analytics inputs and aggregate-only responses."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from meridian.schemas import StrictModel


class AnalyticsRequest(StrictModel):
    snapshot_id: UUID
    as_of: date
    cycle_ids: list[UUID] = Field(default_factory=list, max_length=10)
    scenario_revision_ids: list[UUID] = Field(default_factory=list, max_length=10)
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("cycle_ids", "scenario_revision_ids")
    @classmethod
    def distinct(cls, values: list[UUID]) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("Analytical inputs must be distinct")
        return values


class AnalyticsGapRequest(StrictModel):
    event_id: UUID
    reason: str = Field(min_length=3, max_length=240)


class AnalyticsMoney(StrictModel):
    cycle_id: str
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    exponent: int
    row_count: int
    annualized_merit_minor: str
    merit_first_year_minor: str
    bonus_payable_minor: str
    pool_charge_minor: str
    budget_minor: str
    holdback_minor: str
    remaining_minor: str
    reporting_currency: str
    reporting_pool_charge_minor: str


class AnalyticsScenario(StrictModel):
    scenario_id: str
    revision_id: str
    year: int
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    exponent: int
    annual_minor: str
    baseline_minor: str
    delta_minor: str
    fx_set_id: str
    policy_id: str


class AnalyticsComparison(StrictModel):
    dataset: str
    key: str
    field: str
    expected: str
    observed: str
    matches: bool


class AnalyticsManifestArtifact(StrictModel):
    name: str
    sha256: str
    rows_sha256: str
    row_count: int
    schema_version: int


class AnalyticsRunView(StrictModel):
    id: str
    snapshot_id: str
    snapshot_title: str
    source_batch_id: str
    source_watermark: date
    data_as_of: date
    source_captured_at: datetime
    created_at: datetime
    last_successful_sync: datetime | None
    kind: Literal["SYNC", "GAP_DRILL", "REPAIR"]
    status: Literal["QUEUED", "PASS", "DRIFT", "FAILED"]
    pipeline_status: Literal["PENDING", "CURRENT", "DRIFT", "OUTAGE", "HISTORICAL"]
    model_version: str
    model_sha256: str
    inputs_sha256: str
    result_sha256: str | None
    parent_run_id: str | None
    dropped_event_id: str | None
    attempts: int
    job_error: str | None
    worker_last_seen: datetime | None
    cloud_validation: Literal["UNVERIFIED"] = "UNVERIFIED"
    headcount: int | None
    fte: str | None
    employee_versions: int | None
    compensation: list[AnalyticsMoney]
    scenarios: list[AnalyticsScenario]
    comparisons: list[AnalyticsComparison]
    artifacts: list[AnalyticsManifestArtifact]
    delivery_event_ids: list[str]
    historical: bool


class AnalyticsList(StrictModel):
    items: list[AnalyticsRunView]
    next_cursor: str | None


class AnalyticsChoice(StrictModel):
    id: str
    label: str
    snapshot_id: str | None = None
    as_of: date | None = None


class AnalyticsOptions(StrictModel):
    snapshots: list[AnalyticsChoice]
    cycles: list[AnalyticsChoice]
    scenario_revisions: list[AnalyticsChoice]


class AnalyticsPublic(StrictModel):
    pipeline_status: Literal["EMPTY", "CURRENT", "STALE", "OUTAGE"]
    data_as_of: date | None
    source_watermark: date | None
    last_successful_sync: datetime | None
    headcount: int | None
    fte: str | None
    model_version: str | None
    cloud_validation: Literal["UNVERIFIED"] = "UNVERIFIED"
    suppressed: bool = False


class AnalyticsEvidence(StrictModel):
    id: str
    snapshot_id: str
    data_as_of: date
    model_version: str
    inputs_sha256: str
    result_sha256: str | None
    status: str
    last_successful_sync: datetime | None
    comparison_count: int
    failed_comparisons: int
    source_versions: int | None
    cloud_validation: Literal["UNVERIFIED"] = "UNVERIFIED"
