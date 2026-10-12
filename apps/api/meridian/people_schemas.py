from datetime import date, datetime
from typing import Literal

from pydantic import Field

from meridian.schemas import StrictModel


class Money(StrictModel):
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    minor_units: str
    exponent: int


class PersonPublic(StrictModel):
    id: str
    version_id: str
    display_name: str
    org_id: str
    org_name: str
    manager_id: str | None
    position_id: str
    job_family: str
    grade: str
    region: str
    status: str
    fte: str
    valid_from: date
    valid_to: date | None
    recorded_at: datetime
    source_batch_id: str


class PersonWithPay(PersonPublic):
    annual_pay: Money


class HistoricalQuery(StrictModel):
    as_of: date = date(2027, 6, 30)
    snapshot_id: str | None = Field(default=None, max_length=36)


class PeopleQuery(HistoricalQuery):
    org: str | None = Field(default=None, pattern=r"^D\d{2}$")
    q: str = Field(default="", max_length=80)
    cursor: str | None = Field(default=None, pattern=r"^SYN-\d{6}$")
    limit: int = Field(default=25, ge=1, le=100)


class SnapshotView(StrictModel):
    id: str
    title: str
    source_batch_id: str
    source_watermark: date
    captured_at: datetime
    sha256: str


class PeoplePage(StrictModel):
    items: list[PersonWithPay | PersonPublic]
    next_cursor: str | None
    total: int
    snapshot: SnapshotView
    as_of: date
    scope_label: str
    can_view_pay: bool


class PersonHistory(StrictModel):
    items: list[PersonWithPay | PersonPublic]
    snapshot: SnapshotView
    redacted: bool


class OrgView(StrictModel):
    id: str
    name: str
    region: str
    headcount: int
    parent_id: str | None


class OrgNode(StrictModel):
    id: str
    name: str
    parent_id: str | None
    region: str


class WorkforceSummary(StrictModel):
    headcount: int | None
    fte: str | None
    departments: list[OrgView]
    snapshot: SnapshotView
    as_of: date
    calculated_at: datetime
    metric_definition_version: str = "headcount-active-v1"
    suppressed: bool
    scope_label: str
