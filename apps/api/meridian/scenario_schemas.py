from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from meridian.scenario_operations import OrgID, ScenarioOperation
from meridian.schemas import StrictModel

Reason = Annotated[str, Field(min_length=3, max_length=240)]
Minor = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


class ScenarioRequest(StrictModel):
    title: str = Field(min_length=3, max_length=100)
    base_snapshot_id: UUID
    policy_id: UUID
    fx_set_id: UUID
    reviewer_id: str = Field(min_length=1, max_length=80)
    year: int = Field(ge=1900, le=2199, strict=True)
    planning_date: date
    scope_orgs: list[OrgID] = Field(min_length=1, max_length=100)
    budget_minor: Minor

    @field_validator("planning_date", mode="before")
    @classmethod
    def explicit_date(cls, value: Any) -> Any:
        if type(value) is date or isinstance(value, str) and len(value) == 10:
            return value
        raise ValueError("ISO calendar date required")


class BranchRequest(StrictModel):
    title: str = Field(min_length=3, max_length=100)
    reviewer_id: str = Field(min_length=1, max_length=80)
    reason: Reason


class OperationsRequest(StrictModel):
    operations: list[ScenarioOperation] = Field(min_length=1, max_length=20)
    reason: Reason


class ReasonRequest(StrictModel):
    reason: Reason


class ReviewRequest(ReasonRequest):
    decision: Literal["approve", "return"]


class ScenarioQuery(StrictModel):
    cursor: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)
    search: str | None = Field(default=None, min_length=1, max_length=100)


class CompareQuery(StrictModel):
    ids: list[UUID] = Field(min_length=2, max_length=3)


class RevisionQuery(StrictModel):
    revision: int | None = Field(default=None, ge=1, le=201)


class MoneyComponents(StrictModel):
    base_minor: str
    benefits_minor: str
    variable_minor: str
    employer_tax_minor: str
    total_minor: str


class DepartmentCost(MoneyComponents):
    org_id: str
    opening_headcount: int
    closing_headcount: int
    opening_fte: str
    closing_fte: str
    average_fte: str


class MonthCost(MoneyComponents):
    month: str
    opening_headcount: int
    closing_headcount: int
    opening_fte: str
    closing_fte: str
    average_fte: str
    departments: list[DepartmentCost]


class ForecastInputs(StrictModel):
    snapshot_id: str
    snapshot_sha256: str
    assignment_sha256: str
    policy_id: str
    policy_sha256: str
    fx_set_id: str
    fx_sha256: str
    scope_orgs: list[str]
    year: int


class CostResult(StrictModel):
    engine_version: str
    model_version: str
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    exponent: int
    inputs: ForecastInputs
    months: list[MonthCost]
    annual: MoneyComponents
    sha256: str


class ForecastDocument(StrictModel):
    scenario_id: str
    revision_id: str
    revision_sha256: str
    operation_model_version: str
    planning_rules_sha256: str
    org_calendar_sha256: str
    source_batch_id: str
    result: CostResult


class ForecastView(StrictModel):
    current: ForecastDocument
    baseline: ForecastDocument
    manifest_sha256: str
    annual_delta_minor: str
    annual_component_delta: MoneyComponents
    monthly_delta_minor: list[str]
    verified: bool


class ScenarioSummary(StrictModel):
    id: str
    title: str
    owner_id: str
    reviewer_id: str
    version: int
    revision: int
    state: Literal["DRAFT", "SUBMITTED", "IN_REVIEW", "APPROVED", "REJECTED", "PROMOTED"]
    base_snapshot_id: str
    policy_id: str
    fx_set_id: str
    parent_scenario_id: str | None
    parent_revision_id: str | None
    year: int
    planning_date: date
    scope_orgs: list[str]
    budget_minor: str
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    exponent: int
    annual_total_minor: str
    forecast_sha256: str
    verified: bool
    manifest_ready: bool
    pending_job: bool
    failed_job: bool
    created_at: datetime


class DecisionView(StrictModel):
    id: str
    revision_id: str
    actor_id: str
    action: str
    reason: str
    created_at: datetime


class RevisionView(StrictModel):
    id: str
    number: int
    author_id: str
    reason: str
    sha256: str
    created_at: datetime


class ScenarioDetail(ScenarioSummary):
    operations: list[ScenarioOperation]
    revisions: list[RevisionView]
    decisions: list[DecisionView]
    rules: dict[str, Any]


class ScenarioPage(StrictModel):
    items: list[ScenarioSummary]
    next_cursor: str | None


class Option(StrictModel):
    id: str
    label: str


class AssumptionOption(Option):
    document: dict[str, Any]


class ReviewerOption(Option):
    scope_orgs: list[str]


class ScenarioOptions(StrictModel):
    snapshots: list[Option]
    policies: list[AssumptionOption]
    fx_sets: list[AssumptionOption]
    reviewers: list[ReviewerOption]
    scope_orgs: list[Option]


class CompareItem(StrictModel):
    scenario: ScenarioSummary
    result: CostResult
    annual_delta_minor: str
    monthly_delta_minor: list[str]


class CompareView(StrictModel):
    items: list[CompareItem]


class BaselineView(StrictModel):
    id: str
    scenario_id: str
    revision_id: str
    manifest_sha256: str
    created_at: datetime


class ManifestView(StrictModel):
    sha256: str
    document: dict[str, Any]
