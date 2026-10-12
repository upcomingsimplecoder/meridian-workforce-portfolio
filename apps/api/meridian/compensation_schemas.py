"""Allowlisted compensation requests and projections; no caller-owned auth fields."""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from meridian.compensation_engine import CycleTerms, Guideline
from meridian.people_schemas import Money
from meridian.schemas import StrictModel

Minor = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,19})$")]
EmployeeId = Annotated[str, Field(pattern=r"^SYN-[0-9]{6}$")]
OrgId = Annotated[str, Field(pattern=r"^D[0-9]{2}$")]
PrincipalId = Annotated[str, Field(min_length=1, max_length=80)]
Currency = Literal["USD", "EUR", "GBP", "JPY", "KWD"]
Reason = Annotated[str, Field(min_length=3, max_length=240)]
CycleState = Literal[
    "CONFIGURING",
    "OPEN",
    "REVIEWING",
    "FINAL_APPROVAL",
    "LOCKED",
    "EXPORTED",
    "RECONCILED",
    "FAILED_EXPORT",
    "CANCELLED",
]
ProposalState = Literal["DRAFT", "SUBMITTED", "APPROVAL_PENDING", "APPROVED", "RETURNED", "LOCKED"]


class TermsRequest(CycleTerms):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    @field_validator(
        "eligibility_date",
        "merit_effective_date",
        "bonus_service_start",
        "bonus_service_end",
        "bonus_payment_date",
        mode="before",
    )
    @classmethod
    def calendar_date(cls, value: Any) -> Any:
        if type(value) is date or isinstance(value, str) and len(value) == 10:
            return value
        raise ValueError("ISO calendar date required")


class PoolRequest(StrictModel):
    manager_id: PrincipalId
    org_id: OrgId
    currency: Currency
    budget_minor: Minor
    holdback_minor: Minor


class ApprovalAssignment(StrictModel):
    org_id: OrgId
    division_approver_id: PrincipalId
    hr_signoff_id: PrincipalId
    finance_signoff_id: PrincipalId


class CycleCreate(StrictModel):
    title: str = Field(min_length=3, max_length=100)
    snapshot_id: UUID
    guideline_id: UUID
    fx_set_id: UUID
    terms: TermsRequest
    selected_ids: list[EmployeeId] = Field(min_length=1, max_length=5000)
    pools: list[PoolRequest] = Field(min_length=1, max_length=100)
    approvals: list[ApprovalAssignment] = Field(min_length=1, max_length=100)
    reason: Reason

    @field_validator("title", "reason")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("Meaningful text is required")
        return value.strip()


class CompReason(StrictModel):
    reason: Reason

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("A meaningful reason is required")
        return value.strip()


class AwardRequest(CompReason):
    merit_minor: Minor
    bonus_minor: Minor
    comment: str = Field(max_length=500)


class CompReview(CompReason):
    revision_id: UUID
    decision: Literal["approve", "return"]
    acknowledged_exceptions: list[
        Literal["MERIT_GUIDELINE_EXCEPTION", "BONUS_GUIDELINE_EXCEPTION", "POOL_HOLDBACK_RELEASE"]
    ] = Field(default_factory=list, max_length=3)


class CompQuery(StrictModel):
    cursor: UUID | None = None
    limit: int = Field(default=25, ge=1, le=100)
    search: str | None = Field(default=None, min_length=1, max_length=100)


class CompReviewStart(CompReason):
    revision_id: UUID


class CompRevisionQuery(StrictModel):
    revision: int | None = Field(default=None, ge=1, le=101)


class CompMonth(StrictModel):
    month: str
    merit_cost_minor: str


class AwardInputs(StrictModel):
    eligibility_sha256: str
    guideline_sha256: str
    fx_set_id: str
    fx_sha256: str
    terms: TermsRequest
    terms_sha256: str


class AwardView(StrictModel):
    engine_version: str
    model_version: str
    employee_id: str
    org_id: str
    currency: Currency
    currency_exponent: int
    merit_minor: str
    merit_reference_base_pay_minor: str
    effective_base_pay_minor: str | None
    resulting_base_pay_minor: str | None
    bonus_target_minor: str
    annualized_merit_minor: str
    merit_first_year_minor: str
    merit_months: list[CompMonth]
    bonus_service_fte_days: str
    bonus_service_calendar_days: int
    bonus_payable_minor: str
    pool_charge_minor: str
    reporting_currency: Currency
    reporting_pool_charge_minor: str
    blockers: list[str]
    exceptions: list[str]
    warnings: list[str]
    inputs: AwardInputs
    sha256: str


class CompStepView(StrictModel):
    ordinal: int
    role: Literal["DIVISION", "HR", "FINANCE"]
    assigned_id: str
    capability: str
    required_exceptions: list[str]
    approved: bool
    decision_id: str | None
    active: bool


class CompRevisionView(StrictModel):
    id: str
    number: int
    kind: Literal["INITIAL", "EDIT", "RETURN"]
    author_id: str
    reason: str
    sha256: str
    created_at: datetime


class CompDecisionView(StrictModel):
    id: str
    revision_id: str
    ordinal: int
    round_id: str | None
    actor_id: str
    expected_version: int
    action: str
    reason: str
    acknowledged_exceptions: list[str]
    conflict_check_sha256: str
    created_at: datetime


class ProposalView(StrictModel):
    id: str
    cycle_id: str
    employee_id: str
    display_name: str
    proposer_id: str
    pool_id: str
    state: ProposalState
    version: int
    revision: int
    current_revision: int
    revision_id: str
    base_pay: Money
    fte: str
    comment: str
    award: AwardView
    revision_sha256: str
    verified: bool
    steps: list[CompStepView]
    revisions: list[CompRevisionView]
    decisions: list[CompDecisionView]


class PoolView(StrictModel):
    id: str
    manager_id: str
    org_id: str
    currency: Currency
    cost_center: str
    budget_minor: str
    holdback_minor: str
    award_count: int
    utilized_minor: str
    available_without_holdback_minor: str
    remaining_total_minor: str
    holdback_required_minor: str
    blockers: list[str]
    exceptions: list[str]
    warnings: list[str]


class CycleView(StrictModel):
    id: str
    title: str
    owner_id: str
    snapshot_id: str
    guideline_id: str
    guideline: Guideline
    guideline_sha256: str
    fx_set_id: str
    authz_policy_id: str
    inputs_sha256: str
    terms: TermsRequest
    state: CycleState
    version: int
    current_round_id: str | None
    visible_proposals: int
    configured_population_count: int | None
    ineligible_count: int | None
    pools: list[PoolView]
    export_ready: bool
    export_failed: bool
    created_at: datetime


class CyclePage(StrictModel):
    items: list[CycleView]
    next_cursor: str | None


class ProposalPage(StrictModel):
    items: list[ProposalView]
    total: int
    next_cursor: str | None


class EligibilityView(StrictModel):
    employee_id: str
    eligible: bool
    proposer_id: str | None
    reason_codes: list[str]
    sha256: str


class EligibilityPage(StrictModel):
    items: list[EligibilityView]
    next_cursor: str | None
    total: int


class CompOption(StrictModel):
    id: str
    label: str
    scopes: list[str] = Field(default_factory=list)


class CompOptions(StrictModel):
    snapshots: list[CompOption]
    guidelines: list[CompOption]
    fx_sets: list[CompOption]
    managers: list[CompOption]
    division_approvers: list[CompOption]
    hr_signers: list[CompOption]
    finance_signers: list[CompOption]


class CompExportView(StrictModel):
    cycle_id: str
    version: int
    manifest_sha256: str
    csv_sha256: str
    row_count: int
    download_token: str
    expires_in_seconds: int
    document: dict[str, Any]


class CompEvidenceView(StrictModel):
    cycle_id: str
    title: str
    state: CycleState
    snapshot_id: str
    guideline_id: str
    fx_set_id: str
    authz_policy_id: str
    inputs_sha256: str
    locked_sha256: str | None
    manifest_sha256: str | None
    row_count: int | None
    totals_by_currency: dict[str, dict[str, str]] | None
    signature_verified: bool | None
    decision_count: int
    reconciled: bool
    evidence_scope: Literal["redacted source/export evidence; no person data"] = (
        "redacted source/export evidence; no person data"
    )


class CompEvidencePage(StrictModel):
    items: list[CompEvidenceView]
    next_cursor: str | None


class CompEvidenceQuery(StrictModel):
    cursor: UUID | None = None
    limit: int = Field(default=25, ge=1, le=100)


class EligibilityQuery(StrictModel):
    cursor: EmployeeId | None = None
    limit: int = Field(default=25, ge=1, le=100)
