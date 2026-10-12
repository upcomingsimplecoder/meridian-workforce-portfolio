"""Effective organization coverage from a sealed snapshot; no current-org fallback."""

from dataclasses import dataclass
from datetime import date

from meridian.finance_engine import REGIONS, FinanceInputError


@dataclass(frozen=True)
class OrgPeriod:
    org_id: str
    region: str
    start: date
    end: date | None


@dataclass(frozen=True)
class OrgCalendar:
    periods: tuple[OrgPeriod, ...]

    def region_at(self, org_id: str, day: date) -> str:
        matches = [
            p for p in self.periods if p.org_id == org_id and p.start <= day < (p.end or date.max)
        ]
        if len(matches) != 1 or matches[0].region not in REGIONS:
            raise FinanceInputError("ORGANIZATION_NOT_EFFECTIVE_AT_OPERATION_DATE")
        return matches[0].region

    def segments(self, org_id: str, start: date, end: date) -> list[tuple[date, date, str]]:
        boundaries = sorted(
            {start, end}
            | {
                d
                for p in self.periods
                if p.org_id == org_id
                for d in (p.start, p.end)
                if d is not None and start < d < end
            }
        )
        return [
            (left, right, self.region_at(org_id, left))
            for left, right in zip(boundaries, boundaries[1:], strict=False)
        ]

    @property
    def org_ids(self) -> set[str]:
        return {p.org_id for p in self.periods}
