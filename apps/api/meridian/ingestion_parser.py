"""Deterministic, bounded synthetic input validation. Rejected values never enter errors."""

import csv
import io
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from meridian.schemas import StrictModel
from meridian.synthetic_names import fictional_name

MAX_ROWS = 10000
MAX_BOUNDARIES = 256


class SourceRow(StrictModel):
    employee_id: str = Field(pattern=r"^SYN-[0-9]{6}$")
    display_name: str = Field(min_length=3, max_length=80)
    position_id: str = Field(pattern=r"^POS-[0-9]{6}$")
    org_id: str = Field(pattern=r"^D[0-9]{2}$")
    manager_id: str | None = Field(default=None, pattern=r"^SYN-[0-9]{6}$")
    job_family: str = Field(pattern=r"^[A-Za-z][A-Za-z &/-]{0,49}$")
    grade: str = Field(pattern=r"^L[1-7]$")
    region: Literal["Americas", "Europe", "Asia Pacific", "Middle East"]
    status: Literal["ACTIVE", "EXITED"]
    annual_pay_minor: str = Field(pattern=r"^[0-9]{1,20}$")
    currency: Literal["USD", "EUR", "GBP", "JPY", "KWD"]
    fte: str = Field(pattern=r"^(?:0\.[0-9]{1,4}|1(?:\.0{1,4})?)$")
    valid_from: date
    valid_to: date | None = None

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def iso_dates_only(cls, value: Any) -> Any:
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
        ):
            raise ValueError("Effective dates must be ISO date strings")
        return value

    @model_validator(mode="after")
    def validate_semantics(self) -> "SourceRow":
        expected = fictional_name(int(self.employee_id[-6:]))
        if self.display_name not in {expected, "Synthetic Worker " + self.employee_id[-6:]}:
            raise ValueError("Name must match this generated fictional identity")
        if Decimal(self.fte) <= 0:
            raise ValueError("FTE must be positive")
        if not date(1900, 1, 1) <= self.valid_from <= date(2200, 12, 31):
            raise ValueError("Date outside supported range")
        if self.valid_to is not None and not self.valid_from < self.valid_to <= date(2201, 1, 1):
            raise ValueError("Invalid effective interval")
        self.annual_pay_minor = str(int(self.annual_pay_minor))
        self.fte = format(Decimal(self.fte), ".4f")
        return self


FIELDS = list(SourceRow.model_fields)


@dataclass(frozen=True)
class SourceError:
    row_number: int
    field: str
    code: str
    summary: str


@dataclass
class ParsedSource:
    total: int
    records: list[tuple[int, dict[str, Any]]]
    errors: list[SourceError]


def merge_histories(
    base: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in base:
        grouped[row["employee_id"]].append(dict(row))
    for row in sorted(incoming, key=lambda r: (r["employee_id"], r["valid_from"])):
        start, end = row["valid_from"], row.get("valid_to") or "9999-12-31"
        retained = []
        for old in grouped[row["employee_id"]]:
            old_start, old_end = old["valid_from"], old.get("valid_to") or "9999-12-31"
            if old_start >= end or start >= old_end:
                retained.append(old)
            else:
                if old_start < start:
                    retained.append(old | {"valid_to": start})
                if end < old_end:
                    retained.append(old | {"valid_from": end})
        retained.append(dict(row))
        grouped[row["employee_id"]] = retained
    return sorted(
        (row for rows in grouped.values() for row in rows),
        key=lambda r: (r["employee_id"], r["valid_from"]),
    )


def graph_defects(rows: list[dict[str, Any]]) -> set[str]:
    boundaries = sorted(
        {row["valid_from"] for row in rows}
        | {row["valid_to"] for row in rows if row.get("valid_to")}
    )
    if len(boundaries) > MAX_BOUNDARIES:
        raise ValueError("Too many effective boundaries")
    bad: set[str] = set()
    for effective in boundaries:
        graph = {
            row["employee_id"]: row.get("manager_id")
            for row in rows
            if row["status"] == "ACTIVE"
            and row["valid_from"] <= effective < (row.get("valid_to") or "9999-12-31")
        }
        completed: set[str] = set()
        invalid_at_date: set[str] = set()
        for person in graph:
            path: list[str] = []
            positions: set[str] = set()
            current: str | None = person
            while current is not None and current not in completed:
                if current not in graph or current in positions:
                    invalid_at_date.update(path)
                    break
                positions.add(current)
                path.append(current)
                current = graph[current]
            if current in invalid_at_date:
                invalid_at_date.update(path)
            completed.update(path)
        bad.update(invalid_at_date)
    return bad


def org_covers_interval(row: dict[str, Any], intervals: list[tuple[str, str | None]]) -> bool:
    """Require continuous hierarchy coverage across the entire assignment interval."""
    cursor = row["valid_from"]
    end = row["valid_to"] or "9999-12-31"
    for start, stop in sorted(intervals):
        stop = stop or "9999-12-31"
        if start > cursor:
            return False
        if cursor < stop:
            cursor = stop
        if cursor >= end:
            return True
    return False


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def parse_source(
    data: bytes,
    media_type: str,
    watermark: date,
    base: list[dict[str, Any]],
    orgs: set[str],
    org_intervals: dict[str, list[tuple[str, str | None]]] | None = None,
) -> ParsedSource:
    try:
        decoded = data.decode("utf-8-sig", errors="strict")
        if "\x00" in decoded:
            raise ValueError("NUL input")
        if media_type == "text/csv":
            reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
            if (
                reader.fieldnames is None
                or len(reader.fieldnames) != len(FIELDS)
                or set(reader.fieldnames) != set(FIELDS)
            ):
                raise ValueError("Unsupported header")
            raw: list[Any] = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Malformed record")
                raw.append(row)
                if len(raw) > MAX_ROWS:
                    raise ValueError("Input row limit")
        elif media_type == "application/json":
            document = json.loads(decoded, object_pairs_hook=unique_json_object)
            if (
                not isinstance(document, dict)
                or set(document) != {"schema_version", "records"}
                or type(document["schema_version"]) is not int
                or document["schema_version"] != 1
            ):
                raise ValueError("Unsupported schema")
            raw = document["records"]
            if not isinstance(raw, list) or len(raw) > MAX_ROWS:
                raise ValueError("Invalid records")
        else:
            raise ValueError("Unsupported format")
        if not raw:
            raise ValueError("Empty input")
    except (ValueError, UnicodeError, csv.Error, RecursionError):
        return ParsedSource(
            0,
            [],
            [
                SourceError(
                    0,
                    "file",
                    "INVALID_FILE",
                    "Use UTF-8 workforce schema v1, exact columns and at most 10,000 records.",
                )
            ],
        )
    errors: dict[int, SourceError] = {}
    candidates: dict[int, dict[str, Any]] = {}
    for number, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            errors[number] = SourceError(
                number,
                "row",
                "INVALID_ROW",
                "The record must be an object with the exact workforce fields.",
            )
            continue
        normalized = {
            key: unicodedata.normalize("NFKC", value).strip() if isinstance(value, str) else value
            for key, value in item.items()
        }
        if any(
            isinstance(value, str)
            and (
                value.lstrip().startswith(("=", "+", "-", "@"))
                or value.startswith(("\t", "\r", "\n"))
            )
            for value in item.values()
        ):
            errors[number] = SourceError(
                number,
                "row",
                "FORMULA_INPUT",
                "Formula-leading or control-prefixed cells are not allowed.",
            )
            continue
        for optional in ["manager_id", "valid_to"]:
            if normalized.get(optional) == "":
                normalized[optional] = None
        try:
            canonical = SourceRow.model_validate(normalized).model_dump(mode="json")
            if date.fromisoformat(canonical["valid_from"]) > watermark:
                errors[number] = SourceError(
                    number,
                    "valid_from",
                    "AFTER_WATERMARK",
                    "Effective start must not be later than the declared watermark.",
                )
            elif canonical["org_id"] not in orgs:
                errors[number] = SourceError(
                    number,
                    "org_id",
                    "UNKNOWN_ORG",
                    "Department must exist in the pinned source hierarchy.",
                )
            elif org_intervals is not None and not org_covers_interval(
                canonical, org_intervals.get(canonical["org_id"], [])
            ):
                errors[number] = SourceError(
                    number,
                    "org_id",
                    "INACTIVE_ORG",
                    "Department must be effective throughout the assignment interval.",
                )
            else:
                candidates[number] = canonical
        except ValidationError as exc:
            location = exc.errors(include_input=False, include_url=False, include_context=False)[0][
                "loc"
            ]
            field = str(location[0]) if location and str(location[0]) in FIELDS else "row"
            errors[number] = SourceError(
                number,
                field,
                "INVALID_FIELD",
                "Check the synthetic naming convention, field type, currency, money, FTE and date interval.",
            )
    by_person: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for number, row in candidates.items():
        by_person[row["employee_id"]].append((number, row))
    for versions in by_person.values():
        versions.sort(key=lambda item: item[1]["valid_from"])
        longest_end = ""
        longest_number = 0
        for number, row in versions:
            end = row["valid_to"] or "9999-12-31"
            if row["valid_from"] < longest_end:
                for bad_row_number in (longest_number, number):
                    errors[bad_row_number] = SourceError(
                        bad_row_number,
                        "valid_from",
                        "OVERLAPPING_VERSION",
                        "Duplicate or overlapping intervals for one identity are not allowed.",
                    )
            if end > longest_end:
                longest_end, longest_number = end, number
    valid = {number: row for number, row in candidates.items() if number not in errors}
    merged = merge_histories(base, list(valid.values()))
    try:
        bad = graph_defects(merged)
        # A manager's incoming exit can invalidate an unchanged descendant. Point
        # to the changed manager row rather than quarantining an unrelated first row.
        referenced = {row.get("manager_id") for row in merged if row["employee_id"] in bad}
        affected = [
            number
            for number, row in valid.items()
            if row["employee_id"] in bad
            or (row["status"] == "EXITED" and row["employee_id"] in referenced)
        ]
        if bad and not affected and valid:
            affected = [next(iter(valid))]
        for number in affected:
            errors[number] = SourceError(
                number,
                "manager_id",
                "INVALID_REPORTING_GRAPH",
                "Merged history has a missing/inactive manager or a reporting cycle.",
            )
    except ValueError:
        return ParsedSource(
            len(raw),
            [],
            [
                SourceError(
                    0,
                    "file",
                    "HISTORY_LIMIT",
                    "The merged source exceeds the 256 effective-boundary work limit.",
                )
            ],
        )
    return ParsedSource(
        len(raw),
        [(number, row) for number, row in valid.items() if number not in errors],
        list(errors.values()),
    )


def synthetic_public_fields(row: dict[str, Any]) -> dict[str, str | None]:
    # The ingestion preview deliberately excludes financial fields for every persona.
    fields = [
        "employee_id",
        "display_name",
        "org_id",
        "manager_id",
        "valid_from",
        "valid_to",
        "status",
    ]
    return {field: row[field] for field in fields}
