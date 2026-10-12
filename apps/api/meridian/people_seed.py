import random
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from meridian.ingestion_models import SnapshotPublication
from meridian.people_models import (
    AuthzPolicy,
    Employee,
    EmployeeVersion,
    OrgUnit,
    OrgVersion,
    Position,
    PrincipalEmployee,
    Snapshot,
    SourceBatch,
)
from meridian.policy import (
    POLICY_DEFINITION,
    POLICY_ID,
    POLICY_V1_DEFINITION,
    POLICY_V2_DEFINITION,
    POLICY_V3_DEFINITION,
    POLICY_V4_DEFINITION,
    canonical_hash,
)
from meridian.synthetic_names import fictional_name

DEPARTMENTS = [
    "Research & Insights",
    "Business Operations",
    "Technology",
    "Finance",
    "Risk & Controls",
    "Client Services",
    "Strategy",
    "People & Culture",
    "Product",
    "Legal & Governance",
    "Treasury",
    "Corporate Services",
]
REGIONS = ["Americas", "Europe", "Asia Pacific", "Middle East"]
CURRENCIES = ["USD", "EUR", "JPY", "KWD"]
EXPONENTS = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KWD": 3}


def stable_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:meridian:synthetic:" + label))


BASE_SNAPSHOT = stable_id("snapshot-2027-02")
CURRENT_SNAPSHOT = stable_id("snapshot-2027-06")


def workforce_rows() -> list[dict[str, Any]]:
    rng = random.Random(1701)  # noqa: S311 - reproducible synthetic workforce only; credentials use secrets.
    rows: list[dict[str, Any]] = []
    for i in range(1, 5001):
        department = (i - 1) % 12 + 1
        region = (department - 1) % 4
        currency = CURRENCIES[region]
        annual = rng.randrange(60_000, 180_001, 1000)
        if currency == "JPY":
            annual *= 100
        elif currency == "KWD":
            annual //= 3
        rows.append(
            {
                "employee_id": f"SYN-{i:06d}",
                "position_id": f"POS-{i:06d}",
                "display_name": fictional_name(i),
                "org_id": f"D{department:02d}",
                "manager_id": None
                if i <= 12
                else f"SYN-{department if i <= 36 else department + (12 if ((i - 1) // 12) % 2 == 0 else 24):06d}",
                "job_family": [
                    "Research",
                    "Operations",
                    "Engineering",
                    "Finance",
                    "Risk",
                    "Client Services",
                ][(department - 1) % 6],
                "grade": f"L{rng.randint(1, 7)}",
                "region": REGIONS[region],
                "status": "ACTIVE",
                "annual_pay_minor": str(annual * 10 ** EXPONENTS[currency]),
                "currency": currency,
                "fte": "0.8000" if i % 10 == 0 else "1.0000",
                "valid_from": date(2027, 1, 1),
                "valid_to": None,
            }
        )
    return rows


def seed_people(db: Session) -> None:
    for policy_id, definition in (
        ("authz-v1", POLICY_V1_DEFINITION),
        ("authz-v2", POLICY_V2_DEFINITION),
        ("authz-v3", POLICY_V3_DEFINITION),
        ("authz-v4", POLICY_V4_DEFINITION),
        (POLICY_ID, POLICY_DEFINITION),
    ):
        if db.get(AuthzPolicy, policy_id) is None:
            db.add(
                AuthzPolicy(id=policy_id, definition=definition, sha256=canonical_hash(definition))
            )
    if db.get(Snapshot, CURRENT_SNAPSHOT):
        for existing in db.scalars(select(Snapshot).order_by(Snapshot.captured_at, Snapshot.id)):
            if db.get(SnapshotPublication, existing.id) is None:
                db.add(SnapshotPublication(snapshot_id=existing.id))
        return
    for i, name in enumerate(
        ["Advisory & operations", "Enterprise & people", "Product & governance"], 1
    ):
        db.add(OrgUnit(id=f"G{i:02d}", name=name, cost_center=f"GROUP-{i}"))
    for i, name in enumerate(DEPARTMENTS, 1):
        if db.get(OrgUnit, f"D{i:02d}") is None:
            db.add(OrgUnit(id=f"D{i:02d}", name=name, cost_center=f"CC-{i:03d}"))
    rows = workforce_rows()
    if db.get(Employee, "SYN-000001") is None:
        db.execute(insert(Employee), [{"id": row["employee_id"]} for row in rows])
        db.execute(insert(Position), [{"id": row["position_id"]} for row in rows])
        db.add_all(
            [
                PrincipalEmployee(principal_id="manager-a", employee_id="SYN-000001"),
                PrincipalEmployee(principal_id="manager-b", employee_id="SYN-000002"),
            ]
        )
    db.flush()
    for label, snapshot_id, watermark, captured in [
        (
            "February source freeze",
            BASE_SNAPSHOT,
            date(2027, 2, 28),
            datetime(2027, 2, 28, 18, tzinfo=UTC),
        ),
        (
            "June workforce · corrected",
            CURRENT_SNAPSHOT,
            date(2027, 6, 30),
            datetime(2027, 6, 30, 18, tzinfo=UTC),
        ),
    ]:
        versions = []
        for row in rows:
            item = dict(row)
            # A late-arriving leaf transfer plus a reporting-subtree transfer; old snapshot is untouched.
            if snapshot_id == CURRENT_SNAPSHOT and row["employee_id"] in {
                "SYN-000037",
                "SYN-000013",
            }:
                item["valid_to"] = date(2027, 3, 1)
                versions.append(item)
                item = dict(row) | {
                    "valid_from": date(2027, 3, 1),
                    "org_id": "D02",
                    "region": "Europe",
                    "manager_id": "SYN-000014"
                    if row["employee_id"] == "SYN-000037"
                    else "SYN-000002",
                }
            versions.append(item)
        sha = canonical_hash(versions)
        batch_id = stable_id(f"batch-{snapshot_id}")
        db.add(
            SourceBatch(
                id=batch_id,
                source_type="DETERMINISTIC_SEED",
                sha256=sha,
                state="PUBLISHED",
                accepted_count=5000,
                rejected_count=0,
                watermark=watermark,
                actor_id="hr",
                received_at=captured,
                published_at=captured,
            )
        )
        db.flush()
        snapshot = Snapshot(
            id=snapshot_id,
            source_batch_id=batch_id,
            title=label,
            source_watermark=watermark,
            captured_at=captured,
            sha256=sha,
            row_count=5000,
            status="BUILDING",
        )
        db.add(snapshot)
        db.flush()
        db.add_all(
            [
                OrgVersion(
                    snapshot_id=snapshot_id,
                    org_id=f"D{i:02d}",
                    parent_id=f"G{(i - 1) // 4 + 1:02d}",
                    name=name,
                    region=REGIONS[(i - 1) % 4],
                    valid_from=date(2027, 1, 1),
                )
                for i, name in enumerate(DEPARTMENTS, 1)
            ]
        )
        db.add_all(
            [
                OrgVersion(
                    snapshot_id=snapshot_id,
                    org_id=f"G{i:02d}",
                    parent_id=None,
                    name=name,
                    region="Global",
                    valid_from=date(2027, 1, 1),
                )
                for i, name in enumerate(
                    ["Advisory & operations", "Enterprise & people", "Product & governance"], 1
                )
            ]
        )
        db.execute(
            insert(EmployeeVersion),
            [
                dict(row)
                | {
                    "id": stable_id(f"{snapshot_id}-{row['employee_id']}-{row['valid_from']}"),
                    "snapshot_id": snapshot_id,
                    "source_batch_id": batch_id,
                    "recorded_at": captured,
                    "annual_pay_minor": Decimal(row["annual_pay_minor"]),
                    "fte": Decimal(row["fte"]),
                }
                for row in versions
            ],
        )
        db.flush()
        db.add(SnapshotPublication(snapshot_id=snapshot.id))
        snapshot.status = "PUBLISHED"
        db.flush()
