"""Strict qualitative schemas: narrative and context citations, no employee scores."""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from meridian.schemas import StrictModel

EmployeeId = Annotated[str, Field(pattern=r"^SYN-[0-9]{6}$")]
PrincipalId = Annotated[str, Field(min_length=1, max_length=80)]
PacketState = Literal[
    "DRAFT", "SUBMITTED", "CALIBRATING", "CALIBRATED", "RETURNED", "APPROVED", "LOCKED"
]


class TalentReason(StrictModel):
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("A meaningful reason is required")
        return value.strip()


class TalentAssignment(StrictModel):
    employee_id: EmployeeId
    author_id: PrincipalId
    calibrator_id: PrincipalId
    hr_signer_id: PrincipalId

    @model_validator(mode="after")
    def separate(self) -> "TalentAssignment":
        if len({self.author_id, self.calibrator_id, self.hr_signer_id}) != 3:
            raise ValueError("Three independent participants are required")
        return self


class TalentCreate(TalentReason):
    title: str = Field(min_length=3, max_length=100)
    snapshot_id: UUID
    policy_id: UUID
    as_of: date
    due_date: date
    assignments: list[TalentAssignment] = Field(min_length=1, max_length=500)

    @field_validator("as_of", "due_date", mode="before")
    @classmethod
    def calendar(cls, value: Any) -> Any:
        if type(value) is date or isinstance(value, str) and len(value) == 10:
            return value
        raise ValueError("ISO calendar date required")

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("A title is required")
        return value.strip()

    @model_validator(mode="after")
    def dates_and_population(self) -> "TalentCreate":
        if not 1900 <= self.as_of.year <= 2199 or not 0 <= (self.due_date - self.as_of).days <= 366:
            raise ValueError("A bounded review window is required")
        if len({r.employee_id for r in self.assignments}) != len(self.assignments):
            raise ValueError("Duplicate review recipient")
        return self


class TalentEvidenceReference(StrictModel):
    kind: Literal["WORKFORCE_CONTEXT"] = "WORKFORCE_CONTEXT"
    source_version_id: UUID
    observation: str = Field(min_length=10, max_length=240)

    @field_validator("observation")
    @classmethod
    def observation_text(cls, value: str) -> str:
        if len(value.strip()) < 10:
            raise ValueError("Observable synthetic context is required")
        return value.strip()


class StructuredReview(StrictModel):
    accomplishments: str = Field(default="", max_length=2000)
    collaboration: str = Field(default="", max_length=2000)
    development: str = Field(default="", max_length=2000)
    evidence: list[TalentEvidenceReference] = Field(default_factory=list, max_length=10)


class TalentSave(TalentReason):
    review: StructuredReview


class TalentSignature(TalentReason):
    acknowledgement: Literal[True]

    @field_validator("acknowledgement", mode="before")
    @classmethod
    def explicit_boolean(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Explicit human acknowledgement required")
        return value


class TalentReopen(TalentReason):
    packet_id: UUID


class TalentCommentRequest(TalentReason):
    body: str = Field(min_length=3, max_length=500)

    @field_validator("body")
    @classmethod
    def comment_text(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("A comment is required")
        return value.strip()


class TalentQuery(StrictModel):
    cursor: UUID | None = None
    limit: int = Field(default=25, ge=1, le=100)
    search: str = Field(default="", max_length=100)


class TalentRevisionQuery(StrictModel):
    revision: int | None = Field(default=None, ge=1, le=101)
    comment_cursor: UUID | None = None
    decision_cursor: int | None = Field(default=None, ge=0, le=10000)


class TalentOption(StrictModel):
    id: str
    label: str


class TalentOptions(StrictModel):
    snapshots: list[TalentOption]
    policies: list[TalentOption]
    authors: list[TalentOption]
    calibrators: list[TalentOption]
    hr_signers: list[TalentOption]


class TalentCycleView(StrictModel):
    id: UUID
    title: str
    owner_id: str
    snapshot_id: UUID
    snapshot_sha256: str
    policy_id: UUID
    authz_policy_id: str
    inputs_sha256: str
    as_of: date
    due_date: date
    state: Literal["CONFIGURING", "ACTIVE", "LOCKED"]
    version: int
    lock_number: int
    population_count: int | None
    visible_packets: int
    states: dict[str, int]
    can_control: bool
    current_export_ready: bool
    current_export_failed: bool
    created_at: datetime


class TalentCyclePage(StrictModel):
    items: list[TalentCycleView]
    next_cursor: UUID | None


class PacketRevisionView(StrictModel):
    id: UUID
    number: int
    kind: str
    author_id: str
    reason: str
    sha256: str
    parent_sha256: str | None
    created_at: datetime


class TalentDecisionView(StrictModel):
    id: UUID
    revision_id: UUID
    action: str
    actor_id: str
    reason: str
    acknowledgement: bool
    request_id: UUID
    created_at: datetime


class TalentCommentView(StrictModel):
    id: UUID
    revision_id: UUID
    author_id: str
    body: str
    sha256: str
    created_at: datetime


class CalibrationView(StrictModel):
    revision_id: UUID
    before_revision_id: UUID
    actor_id: str
    reason: str
    before_sha256: str
    after_sha256: str
    created_at: datetime


class TalentContext(StrictModel):
    snapshot_id: UUID
    source_batch_id: UUID
    source_version_id: UUID
    employee_id: EmployeeId
    display_name: str
    org_id: str
    manager_id: EmployeeId | None
    job_family: str
    grade: str
    valid_from: date
    valid_to: date | None


class PacketView(StrictModel):
    id: UUID
    cycle_id: UUID
    employee_id: EmployeeId
    display_name: str
    author_id: str
    calibrator_id: str
    hr_signer_id: str
    source_version_id: UUID
    context: TalentContext
    context_sha256: str
    state: PacketState
    version: int
    current_revision: int
    revision: int
    revision_id: UUID
    review: StructuredReview
    revision_sha256: str
    checklist: list[str]
    can_edit: bool
    can_calibrate: bool
    allowed_actions: list[str]
    revisions: list[PacketRevisionView]
    decisions: list[TalentDecisionView]
    signatures: list[TalentDecisionView]
    decisions_next_version: int | None
    calibrations: list[CalibrationView]
    comments: list[TalentCommentView]
    comments_next_cursor: UUID | None


class PacketSummary(StrictModel):
    id: UUID
    cycle_id: UUID
    employee_id: EmployeeId
    display_name: str
    author_id: str
    calibrator_id: str
    hr_signer_id: str
    state: PacketState
    version: int
    current_revision: int
    revision_sha256: str


class PacketPage(StrictModel):
    items: list[PacketSummary]
    total: int
    next_cursor: UUID | None


class TalentExportView(StrictModel):
    cycle_id: UUID
    lock_number: int
    lock_sha256: str
    export_sha256: str
    row_count: int
    current: bool
    expires_in_seconds: int
    download_path: str


class TalentEvidenceView(StrictModel):
    cycle_id: UUID
    title: str
    state: str
    snapshot_id: UUID
    snapshot_sha256: str
    policy_id: UUID
    authz_policy_id: str
    inputs_sha256: str
    lock_number: int
    lock_sha256: str | None
    export_sha256: str | None
    row_count: int | None
    signature_verified: bool | None


class TalentEvidencePage(StrictModel):
    items: list[TalentEvidenceView]
    next_cursor: UUID | None
