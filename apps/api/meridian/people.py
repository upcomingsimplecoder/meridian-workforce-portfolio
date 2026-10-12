import csv
import io
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from meridian.analytics_auth import authorize as authorize_analytics
from meridian.errors import DomainError
from meridian.governance import record
from meridian.identity import DB, Actor, CurrentActor, require
from meridian.ingestion_models import SnapshotPublication
from meridian.models import now
from meridian.people_models import EmployeeVersion, OrgVersion, PrincipalEmployee, Snapshot
from meridian.people_schemas import (
    HistoricalQuery,
    Money,
    OrgNode,
    OrgView,
    PeoplePage,
    PeopleQuery,
    PersonHistory,
    PersonPublic,
    PersonWithPay,
    SnapshotView,
    WorkforceSummary,
)
from meridian.people_seed import EXPONENTS

router = APIRouter(prefix="/api/v1", tags=["workforce"])
Filters = Annotated[PeopleQuery, Query()]
HistoryFilters = Annotated[HistoricalQuery, Query()]


def snapshot_view(snapshot: Snapshot) -> SnapshotView:
    return SnapshotView.model_validate(snapshot, from_attributes=True)


def select_snapshot(db: Session, snapshot_id: str | None) -> Snapshot:
    query = select(Snapshot).outerjoin(SnapshotPublication).where(Snapshot.status == "PUBLISHED")
    if snapshot_id:
        query = query.where(Snapshot.id == snapshot_id)
    result = db.scalar(
        query.order_by(
            SnapshotPublication.ordinal.desc().nullslast(), Snapshot.captured_at.desc(), Snapshot.id
        ).limit(1)
    )
    if result is None:
        raise DomainError(404, "snapshot_unavailable", "Published snapshot not found.")
    return result


def visible_query(
    db: Session, current: Actor, snapshot: Snapshot, as_of: date, capability: str = "people.read"
) -> Select[EmployeeVersion]:
    ev = EmployeeVersion
    active = select(ev).where(
        ev.snapshot_id == snapshot.id,
        ev.valid_from <= as_of,
        or_(ev.valid_to.is_(None), ev.valid_to > as_of),
    )
    grants = [g for g in current.grants if capability in current.policy.get(g.role, [])]
    if any(g.role != "manager" and g.scope == "*" for g in grants):
        return active
    # Each manager grant is constrained to a server-provisioned workforce root.
    predicates = []
    if any(g.role == "manager" for g in grants):
        link = db.get(PrincipalEmployee, current.principal.id)
        if link is not None:
            valid = active.subquery("valid_people")
            descendants = (
                select(valid.c.employee_id)
                .where(valid.c.manager_id == link.employee_id)
                .cte(recursive=True)
            )
            descendants = descendants.union(
                select(valid.c.employee_id).join(
                    descendants, valid.c.manager_id == descendants.c.employee_id
                )
            )
            predicates.append(ev.employee_id.in_(select(descendants.c.employee_id)))
    permitted_orgs = [g.scope for g in grants if g.role != "manager" and g.scope != "*"]
    if permitted_orgs:
        predicates.append(ev.org_id.in_(permitted_orgs))
    return active.where(or_(*predicates) if predicates else ev.employee_id == "NO-SCOPE")


def scope_label(current: Actor) -> str:
    if any(
        g.scope == "*" and "people.read" in current.policy.get(g.role, []) for g in current.grants
    ):
        return "Organization-wide · permitted fields only"
    if any(g.role == "manager" for g in current.grants):
        return "Your reporting descendants at the selected effective date"
    if any(g.scope == "*" for g in current.grants):
        return "Organization-wide · permitted fields only"
    return "Assigned departments · " + ", ".join(sorted({g.scope for g in current.grants}))


def org_names(db: Session, snapshot: Snapshot, as_of: date) -> dict[str, str]:
    return {
        key: value
        for key, value in db.execute(
            select(OrgVersion.org_id, OrgVersion.name).where(
                OrgVersion.snapshot_id == snapshot.id,
                OrgVersion.valid_from <= as_of,
                or_(OrgVersion.valid_to.is_(None), OrgVersion.valid_to > as_of),
            )
        )
    }


def project(
    person: EmployeeVersion, names: dict[str, str], can_pay: bool
) -> PersonWithPay | PersonPublic:
    values: dict[str, Any] = {
        field: getattr(person, field)
        for field in [
            "display_name",
            "org_id",
            "manager_id",
            "position_id",
            "job_family",
            "grade",
            "region",
            "status",
            "valid_from",
            "valid_to",
            "recorded_at",
            "source_batch_id",
        ]
    }
    values.update(
        id=person.employee_id,
        version_id=person.id,
        org_name=names.get(person.org_id, person.org_id),
        fte=format(person.fte, ".4f"),
    )
    if can_pay:
        return PersonWithPay(
            **values,
            annual_pay=Money.model_validate(
                {
                    "currency": person.currency,
                    "minor_units": format(person.annual_pay_minor, ".0f"),
                    "exponent": EXPONENTS[person.currency],
                }
            ),
        )
    return PersonPublic(**values)


def filters_query(
    db: Session,
    current: Actor,
    snapshot: Snapshot,
    filters: PeopleQuery,
    capability: str = "people.read",
) -> Select[EmployeeVersion]:
    query = visible_query(db, current, snapshot, filters.as_of, capability)
    if filters.org:
        query = query.where(EmployeeVersion.org_id == filters.org)
    if filters.q:
        query = query.where(
            or_(
                EmployeeVersion.display_name.icontains(filters.q, autoescape=True),
                EmployeeVersion.employee_id.icontains(filters.q, autoescape=True),
            )
        )
    return query


def pay_ids(
    db: Session, current: Actor, snapshot: Snapshot, as_of: date, rows: list[EmployeeVersion]
) -> set[str]:
    if not rows or "people.pay" not in current.capabilities:
        return set()
    return set(
        db.scalars(
            visible_query(db, current, snapshot, as_of, "people.pay")
            .with_only_columns(EmployeeVersion.id)
            .where(EmployeeVersion.id.in_([row.id for row in rows]))
        )
    )


def deny_object(db: Session, current: Actor, request: Request, employee_id: str) -> None:
    record(
        db,
        current.principal.id,
        "security.object_denied",
        "employee",
        employee_id[:100],
        request.state.request_id,
        False,
    )
    db.commit()
    raise DomainError(
        404, "person_unavailable", "Person not found in your permitted scope at this date."
    )


@router.get("/snapshots", response_model=list[SnapshotView])
def snapshots(
    current: CurrentActor,
    db: DB,
    request: Request,
    source_search: Annotated[str, Query(max_length=100)] = "",
    snapshot_id: UUID | None = None,
) -> list[SnapshotView]:
    if not current.capabilities.intersection({"people.read", "analytics.read", "audit.read"}):
        require(current, "people.read", db, request)
    return [snapshot_view(row) for row in published_snapshots(db, source_search, snapshot_id)]


def published_snapshots(
    db: Session, source_search: str = "", snapshot_id: UUID | None = None
) -> list[Snapshot]:
    query = select(Snapshot).outerjoin(SnapshotPublication).where(Snapshot.status == "PUBLISHED")
    if source_search.strip():
        query = query.where(Snapshot.title.icontains(source_search.strip(), autoescape=True))
    if snapshot_id:
        query = query.where(Snapshot.id == str(snapshot_id))
    return list(
        db.scalars(
            query.order_by(
                SnapshotPublication.ordinal.desc().nullslast(),
                Snapshot.captured_at.desc(),
                Snapshot.id,
            ).limit(100)
        )
    )


@router.get("/people", response_model=PeoplePage)
def people(current: CurrentActor, db: DB, request: Request, filters: Filters) -> PeoplePage:
    require(current, "people.read", db, request)
    snapshot = select_snapshot(db, filters.snapshot_id)
    query = filters_query(db, current, snapshot, filters)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if filters.cursor:
        query = query.where(EmployeeVersion.employee_id > filters.cursor)
    rows = list(db.scalars(query.order_by(EmployeeVersion.employee_id).limit(filters.limit + 1)))
    allowed_pay = pay_ids(db, current, snapshot, filters.as_of, rows)
    names = org_names(db, snapshot, filters.as_of)
    return PeoplePage(
        items=[project(row, names, row.id in allowed_pay) for row in rows[: filters.limit]],
        next_cursor=rows[filters.limit - 1].employee_id if len(rows) > filters.limit else None,
        total=total,
        snapshot=snapshot_view(snapshot),
        as_of=filters.as_of,
        scope_label=scope_label(current),
        can_view_pay=bool(allowed_pay),
    )


def safe_cell(value: str) -> str:
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
        or value.startswith(("\t", "\r", "\n"))
        else value
    )


@router.get(
    "/people/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def export_people(current: CurrentActor, db: DB, request: Request, filters: Filters) -> Response:
    require(current, "people.export", db, request)
    snapshot = select_snapshot(db, filters.snapshot_id)
    query = filters_query(db, current, snapshot, filters, "people.export")
    rows = list(db.scalars(query.order_by(EmployeeVersion.employee_id).limit(5001)))
    if len(rows) > 5000:
        raise DomainError(422, "export_limit", "Narrow the export to at most 5,000 records.")
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    allowed_pay = pay_ids(db, current, snapshot, filters.as_of, rows)
    pay = bool(allowed_pay)
    columns = [
        "synthetic_id",
        "display_name",
        "org_id",
        "manager_id",
        "fte",
        "snapshot_id",
        "as_of",
    ]
    if pay:
        columns += ["annual_pay_minor", "currency"]
    writer.writerow(columns)
    for row in rows:
        values = [
            row.employee_id,
            row.display_name,
            row.org_id,
            row.manager_id or "",
            format(row.fte, ".4f"),
            snapshot.id,
            filters.as_of.isoformat(),
        ]
        if pay:
            values += (
                [format(row.annual_pay_minor, ".0f"), row.currency]
                if row.id in allowed_pay
                else ["", ""]
            )
        writer.writerow([safe_cell(value) for value in values])
    record(
        db,
        current.principal.id,
        "people.export",
        "snapshot",
        snapshot.id,
        request.state.request_id,
        emit=False,
    )
    db.commit()
    return Response(
        stream.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="meridian-synthetic-workforce.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/people/{employee_id}", response_model=PersonWithPay | PersonPublic)
def person(
    employee_id: str,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: HistoryFilters,
) -> PersonWithPay | PersonPublic:
    require(current, "people.read", db, request)
    snapshot = select_snapshot(db, filters.snapshot_id)
    row = db.scalar(
        visible_query(db, current, snapshot, filters.as_of).where(
            EmployeeVersion.employee_id == employee_id
        )
    )
    if row is None:
        deny_object(db, current, request, employee_id)
    return project(
        cast(EmployeeVersion, row),
        org_names(db, snapshot, filters.as_of),
        bool(pay_ids(db, current, snapshot, filters.as_of, [cast(EmployeeVersion, row)])),
    )


@router.get("/people/{employee_id}/history", response_model=PersonHistory)
def history(
    employee_id: str,
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: HistoryFilters,
) -> PersonHistory:
    require(current, "people.read", db, request)
    snapshot = select_snapshot(db, filters.snapshot_id)
    if (
        db.scalar(
            visible_query(db, current, snapshot, filters.as_of).where(
                EmployeeVersion.employee_id == employee_id
            )
        )
        is None
    ):
        deny_object(db, current, request, employee_id)
    rows = list(
        db.scalars(
            select(EmployeeVersion)
            .where(
                EmployeeVersion.snapshot_id == snapshot.id,
                EmployeeVersion.employee_id == employee_id,
                EmployeeVersion.valid_from <= filters.as_of,
            )
            .order_by(EmployeeVersion.valid_from)
        )
    )
    items: list[PersonWithPay | PersonPublic] = []
    for row in rows:
        visible_at = (
            filters.as_of
            if row.valid_to is None or row.valid_to > filters.as_of
            else row.valid_from
        )
        if (
            db.scalar(
                visible_query(db, current, snapshot, visible_at).where(EmployeeVersion.id == row.id)
            )
            is not None
        ):
            items.append(
                project(
                    row,
                    org_names(db, snapshot, visible_at),
                    bool(pay_ids(db, current, snapshot, visible_at, [row])),
                )
            )
    return PersonHistory(
        items=items,
        snapshot=snapshot_view(snapshot),
        redacted=len(items) != len(rows)
        or any(not isinstance(item, PersonWithPay) for item in items),
    )


@router.get("/workforce/summary", response_model=WorkforceSummary)
def summary(
    current: CurrentActor,
    db: DB,
    request: Request,
    filters: HistoryFilters,
) -> WorkforceSummary:
    aggregate_only = (
        "analytics.read" in current.capabilities and "people.read" not in current.capabilities
    )
    if aggregate_only:
        authorize_analytics(current, "analytics.read", db, request)
    else:
        require(current, "people.read", db, request)
    snapshot = select_snapshot(db, filters.snapshot_id)
    if aggregate_only:
        query = select(EmployeeVersion).where(
            EmployeeVersion.snapshot_id == snapshot.id,
            EmployeeVersion.valid_from <= filters.as_of,
            or_(EmployeeVersion.valid_to.is_(None), EmployeeVersion.valid_to > filters.as_of),
        )
    else:
        query = visible_query(db, current, snapshot, filters.as_of)
    query = query.where(EmployeeVersion.status == "ACTIVE")
    valid = query.subquery()
    count, fte = db.execute(
        select(func.count(), func.coalesce(func.sum(valid.c.fte), Decimal(0)))
    ).one()
    groups = db.execute(
        select(valid.c.org_id, func.count()).group_by(valid.c.org_id).order_by(valid.c.org_id)
    ).all()
    names = org_names(db, snapshot, filters.as_of)
    org_data = {
        key: (region, parent)
        for key, region, parent in db.execute(
            select(OrgVersion.org_id, OrgVersion.region, OrgVersion.parent_id).where(
                OrgVersion.snapshot_id == snapshot.id,
                OrgVersion.valid_from <= filters.as_of,
                or_(OrgVersion.valid_to.is_(None), OrgVersion.valid_to > filters.as_of),
            )
        )
    }
    suppressed = aggregate_only and (count < 5 or any(n < 5 for _, n in groups))
    return WorkforceSummary(
        headcount=count if not (aggregate_only and count < 5) else None,
        fte=format(fte, ".4f") if not (aggregate_only and count < 5) else None,
        departments=[]
        if suppressed
        else [
            OrgView(
                id=org,
                name=names.get(org, org),
                region=org_data.get(org, ("Unspecified", None))[0],
                parent_id=org_data.get(org, ("Unspecified", None))[1],
                headcount=n,
            )
            for org, n in groups
        ],
        snapshot=snapshot_view(snapshot),
        as_of=filters.as_of,
        calculated_at=now(),
        suppressed=suppressed,
        scope_label="Governed aggregates · minimum cohort 5"
        if aggregate_only
        else scope_label(current),
    )


@router.get("/org-units", response_model=list[OrgNode])
def org_units(
    current: CurrentActor, db: DB, request: Request, filters: HistoryFilters
) -> list[OrgNode]:
    metrics = summary(current, db, request, filters)
    rows = list(
        db.scalars(
            select(OrgVersion).where(
                OrgVersion.snapshot_id == metrics.snapshot.id,
                OrgVersion.valid_from <= filters.as_of,
                or_(OrgVersion.valid_to.is_(None), OrgVersion.valid_to > filters.as_of),
            )
        )
    )
    by_id = {row.org_id: row for row in rows}
    permitted = {department.id for department in metrics.departments}
    pending = list(permitted)
    while pending:
        node = by_id.get(pending.pop())
        if node and node.parent_id and node.parent_id not in permitted:
            permitted.add(node.parent_id)
            pending.append(node.parent_id)
    return [
        OrgNode(id=row.org_id, name=row.name, parent_id=row.parent_id, region=row.region)
        for row in rows
        if row.org_id in permitted
    ]
