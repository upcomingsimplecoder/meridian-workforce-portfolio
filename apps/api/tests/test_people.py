"""Real PostgreSQL policy, history, export and sealing tests; no route authorization mocks."""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from meridian import identity
from meridian.ingestion_models import SnapshotPublication
from meridian.models import AuditEvent, RoleAssignment, now, uid
from meridian.people import safe_cell
from meridian.people_models import AuthzPolicy, EmployeeVersion, OrgVersion, Snapshot, SourceBatch
from meridian.people_seed import BASE_SNAPSHOT, CURRENT_SNAPSHOT, workforce_rows
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


def get(client, path="/api/v1/people", **params):
    return client.get(
        path, params={"snapshot_id": CURRENT_SNAPSHOT, "as_of": "2027-06-30"} | params
    )


def test_deterministic_seed_and_scoped_keyset_pages(client, login, db):
    login()
    assert workforce_rows() == workforce_rows()
    assert db.scalar(select(func.count()).select_from(EmployeeVersion)) == 10002
    first = get(client).json()
    assert first["total"] == 5000
    assert len(first["items"]) == 25
    assert first["next_cursor"] == "SYN-000025"
    second = get(client, cursor=first["next_cursor"]).json()
    assert second["items"][0]["id"] == "SYN-000026"
    assert set(row["id"] for row in first["items"]).isdisjoint(row["id"] for row in second["items"])
    assert len(get(client, limit=100).json()["items"]) == 100
    assert get(client, cursor="SYN-005000").json()["items"] == []
    metrics = get(client, "/api/v1/workforce/summary").json()
    assert metrics["headcount"] == 5000
    assert metrics["fte"] == "4900.0000"
    assert sum(org["headcount"] for org in metrics["departments"]) == 5000
    assert metrics["metric_definition_version"] == "headcount-active-v1"


def test_late_correction_preserves_published_snapshot_and_effective_history(client, login):
    login()
    path = "/api/v1/people/SYN-000037"
    assert get(client, path, snapshot_id=BASE_SNAPSHOT).json()["org_id"] == "D01"
    assert get(client, path, as_of="2027-02-28").json()["org_id"] == "D01"
    current = get(client, path, as_of="2027-03-01").json()
    assert current["org_id"] == "D02"
    assert current["valid_from"] == "2027-03-01"
    history = get(client, path + "/history").json()
    assert [row["org_id"] for row in history["items"]] == ["D01", "D02"]
    assert history["items"][0]["valid_to"] == "2027-03-01"
    assert history["items"][0]["source_batch_id"] == current["source_batch_id"]
    assert history["redacted"] is False


@pytest.mark.parametrize("persona,before,after", [("manager-a", 416, 207), ("manager-b", 416, 625)])
def test_nested_reporting_transfer_is_effective_dated(client, login, persona, before, after):
    login(persona)
    assert get(client, as_of="2027-02-28").json()["total"] == before
    assert get(client, snapshot_id=BASE_SNAPSHOT).json()["total"] == before
    assert get(client).json()["total"] == after
    # The leaf's own row never changes; its manager's manager transfers across the tree.
    expected = 404 if persona == "manager-a" else 200
    assert get(client, "/api/v1/people/SYN-000049").status_code == expected
    assert get(client, "/api/v1/people/SYN-000049", as_of="2027-02-28").status_code == (
        200 if expected == 404 else 404
    )
    if persona == "manager-b":
        body = get(client, "/api/v1/people/SYN-000049").json()
        assert body["manager_id"] == "SYN-000013"
        assert body["org_id"] == "D01"


@pytest.mark.parametrize("suffix", ["", "/history"])
@pytest.mark.security
def test_cross_manager_objects_are_denied_and_audited(client, login, db, suffix):
    login("manager-a")
    response = get(client, "/api/v1/people/SYN-000014" + suffix)
    assert response.status_code == 404
    assert "annual_pay" not in response.text
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.verb == "security.object_denied")
        )
        == 1
    )
    # Historical access is also rechecked; manager A cannot retrieve the transferred revision.
    history = get(client, "/api/v1/people/SYN-000037/history", as_of="2027-02-28").json()
    assert len(history["items"]) == 1
    assert history["items"][0]["org_id"] == "D01"


@pytest.mark.security
def test_foreign_org_filters_cursors_and_exports_only_narrow(client, login):
    login("manager-a")
    assert get(client, org="D02").json()["total"] == 0
    assert get(client, q="SYN-000014").json()["items"] == []
    rows = list(csv.DictReader(io.StringIO(get(client, "/api/v1/people/export").text)))
    assert len(rows) == 207
    assert {row["org_id"] for row in rows} == {"D01"}
    assert all(row["snapshot_id"] == CURRENT_SNAPSHOT for row in rows)
    assert get(client, "/api/v1/people/export", org="D02").text.count("\n") == 1
    assert get(client, cursor="SYN-000014").json()["total"] == 207
    assert get(client, q="%_").json()["items"] == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/people",
        "/api/v1/people/export",
        "/api/v1/people/SYN-000014",
        "/api/v1/people/SYN-000014/history",
    ],
)
@pytest.mark.parametrize("persona", ["executive", "auditor"])
@pytest.mark.security
def test_raw_routes_forbidden_without_people_capability(client, login, path, persona):
    login(persona)
    response = get(client, path)
    assert response.status_code == 403
    assert "annual_pay" not in response.text
    assert "Synthetic Worker" not in response.text
    assert "display_name" not in response.text
    assert workforce_rows()[0]["display_name"] not in response.text


@pytest.mark.security
def test_steward_field_projection_and_export_denial(client, login):
    login("steward")
    for path in [
        "/api/v1/people",
        "/api/v1/people/SYN-000003",
        "/api/v1/people/SYN-000003/history",
    ]:
        response = get(client, path)
        assert response.status_code == 200
        assert "annual_pay" not in response.text
        assert "currency" not in response.text
        assert "minor_units" not in response.text
    assert get(client, "/api/v1/people/export").status_code == 403


@pytest.mark.security
def test_combined_grants_do_not_amplify_field_or_export_scope(client, login, db):
    login("manager-a")
    db.add(
        RoleAssignment(
            principal_id="manager-a",
            role="steward",
            scope="*",
            effective_from=now() - timedelta(days=1),
        )
    )
    db.commit()
    assert get(client).json()["total"] == 5000
    assert "annual_pay" not in get(client, "/api/v1/people/SYN-000014").json()
    assert "annual_pay" in get(client, "/api/v1/people/SYN-000025").json()
    exported = list(csv.DictReader(io.StringIO(get(client, "/api/v1/people/export").text)))
    assert len(exported) == 207


@pytest.mark.parametrize(
    "route", ["/api/v1/people", "/api/v1/people/export", "/api/v1/people/SYN-000014/history"]
)
@pytest.mark.security
def test_grant_revocation_applies_to_existing_sessions_and_exports(client, login, db, route):
    login("manager-b")
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "manager-b"))
    grant.effective_to = now() - timedelta(seconds=1)
    db.commit()
    assert get(client, route).status_code == 403


@pytest.mark.parametrize(
    "path,field,value",
    [
        ("/api/v1/people", "role", "hr"),
        ("/api/v1/people", "scope", "*"),
        ("/api/v1/people", "limit", "101"),
        ("/api/v1/people", "cursor", "invalid"),
        ("/api/v1/people/export", "owner_id", "hr"),
        ("/api/v1/people/SYN-000014", "role", "hr"),
        ("/api/v1/people/SYN-000014/history", "scope", "*"),
        ("/api/v1/workforce/summary", "q", "Synthetic Worker 000001"),
        ("/api/v1/org-units", "org", "D02"),
    ],
)
@pytest.mark.security
def test_overposting_and_unbounded_filters_rejected(client, login, path, field, value):
    login("manager-a")
    assert get(client, path, **{field: value}).status_code == 422


@pytest.mark.security
def test_executive_aggregate_dto_and_small_cohort_suppression(client, login, db):
    login("executive")
    data = get(client, "/api/v1/workforce/summary").json()
    assert data["headcount"] == 5000
    assert data["fte"] == "4900.0000"
    assert not data["suppressed"]
    assert "row_count" not in data["snapshot"]
    assert "annual_pay" not in str(data)
    assert "Synthetic Worker" not in str(data)
    assert "display_name" not in str(data)
    assert workforce_rows()[0]["display_name"] not in str(data)
    empty = get(client, "/api/v1/workforce/summary", as_of="2026-01-01").json()
    assert empty["headcount"] is None
    assert empty["fte"] is None
    assert empty["suppressed"]
    assert empty["departments"] == []
    assert "row_count" not in client.get("/api/v1/snapshots").text


@pytest.mark.parametrize("route", ["/api/v1/workforce/summary", "/api/v1/org-units"])
@pytest.mark.security
def test_narrowed_executive_grant_cannot_replay_global_workforce_aggregates(
    client, login, db, route
):
    session = login("executive")
    assert get(client, route).status_code == 200
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "executive"))
    grant.scope = "D01"
    db.commit()
    response = get(client, route)
    assert response.status_code == 403
    assert response.json()["type"] == "urn:meridian:problem:analytics_forbidden"
    assert "departments" not in response.json()
    assert "headcount" not in response.json()
    assert not session.revoked
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.actor_id == "executive",
                AuditEvent.verb == "security.analytics_denied",
            )
        )
        == 1
    )
    grant.scope = "*"
    db.commit()
    assert get(client, route).status_code == 200


@pytest.mark.security
def test_org_hierarchy_scoped_to_reporting_tree(client, login):
    login("manager-a")
    nodes = get(client, "/api/v1/org-units").json()
    assert {row["id"] for row in nodes} == {"D01", "G01"}
    assert next(row for row in nodes if row["id"] == "D01")["parent_id"] == "G01"


@pytest.mark.security
def test_policy_unavailable_fails_closed_and_policy_is_sealed(client, login, db, monkeypatch):
    login()
    monkeypatch.setattr(identity, "POLICY_ID", "unavailable-policy")
    assert get(client).status_code == 503
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("UPDATE authz_policy_version SET definition='{}' WHERE id='authz-v1'"))
    assert db.get(AuthzPolicy, "authz-v1").definition["executive"] == ["analytics.read"]


@pytest.mark.parametrize("table", ["employee_version", "org_version", "snapshot"])
@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
@pytest.mark.security
def test_published_data_immutable_in_database(db, table, action):
    statement = f"DELETE FROM {table}" if action == "DELETE" else f"UPDATE {table} SET id=id"
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(statement))


@pytest.fixture
def open_snapshot(db):
    return make_open_snapshot(db)


def make_open_snapshot(db):
    batch = SourceBatch(
        source_type="TEST",
        sha256=uid(),
        state="PREVIEW",
        accepted_count=0,
        rejected_count=0,
        watermark=date(2027, 6, 30),
        actor_id="hr",
    )
    db.add(batch)
    db.flush()
    snapshot = Snapshot(
        source_batch_id=batch.id,
        title="Synthetic constraint fixture",
        source_watermark=batch.watermark,
        sha256=batch.sha256,
        row_count=1,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def copy_version(db, snapshot, **updates):
    original = db.scalar(
        select(EmployeeVersion).where(
            EmployeeVersion.snapshot_id == CURRENT_SNAPSHOT,
            EmployeeVersion.employee_id == "SYN-000001",
        )
    )
    values = {
        column.name: getattr(original, column.name) for column in EmployeeVersion.__table__.columns
    }
    values.update(id=uid(), snapshot_id=snapshot.id, source_batch_id=snapshot.source_batch_id)
    db.add(EmployeeVersion(**(values | updates)))
    db.flush()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/snapshots",
        "/api/v1/scenarios/options",
        "/api/v1/compensation/options",
        "/api/v1/talent/options",
    ],
)
def test_historical_snapshot_search_remains_bounded_after_more_than_100_publications(
    client, login, db, path
):
    login()
    for number in range(101):
        snapshot = make_open_snapshot(db)
        snapshot.title = f"Recent source {number}"
        copy_version(db, snapshot)
        snapshot.status = "PUBLISHED"
        db.flush()
        db.add(SnapshotPublication(snapshot_id=snapshot.id))
    db.flush()
    response = client.get(path)
    assert response.status_code == 200
    rows = response.json() if path.endswith("/snapshots") else response.json()["snapshots"]
    assert len(rows) == 100
    assert CURRENT_SNAPSHOT not in {row["id"] for row in rows}
    searched = client.get(path, params={"source_search": "June workforce"})
    assert searched.status_code == 200
    rows = searched.json() if path.endswith("/snapshots") else searched.json()["snapshots"]
    assert [row["id"] for row in rows] == [CURRENT_SNAPSHOT]
    assert "row_count" not in searched.text
    assert "annual_pay" not in searched.text
    literal = client.get(path, params={"source_search": "%"})
    assert literal.status_code == 200
    assert (literal.json() if path.endswith("/snapshots") else literal.json()["snapshots"]) == []
    assert client.get(path, params={"source_search": "x" * 101}).status_code == 422
    if path.endswith("/snapshots"):
        pinned = client.get(path, params={"snapshot_id": CURRENT_SNAPSHOT})
        assert pinned.status_code == 200
        assert [row["id"] for row in pinned.json()] == [CURRENT_SNAPSHOT]
        assert client.get(path, params={"snapshot_id": "invalid"}).status_code == 422


@pytest.mark.security
@pytest.mark.parametrize("persona", ["manager-a", "executive"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/scenarios/options",
        "/api/v1/compensation/options",
        "/api/v1/talent/options",
    ],
)
def test_snapshot_search_does_not_bypass_domain_authorization(client, login, persona, path):
    login(persona)
    response = client.get(path, params={"source_search": "June workforce"})
    assert response.status_code == 403
    assert "June workforce" not in response.text


@pytest.mark.security
def test_sealed_snapshot_rejects_additional_inserts(db):
    with pytest.raises(IntegrityError), db.begin_nested():
        copy_version(
            db,
            db.get(Snapshot, CURRENT_SNAPSHOT),
            employee_id="SYN-000002",
            valid_from=date(2029, 1, 1),
        )


@pytest.mark.security
def test_overlapping_effective_dates_rejected_but_adjacent_allowed(db, open_snapshot):
    copy_version(db, open_snapshot, valid_to=date(2027, 3, 1))
    with pytest.raises(IntegrityError), db.begin_nested():
        copy_version(db, open_snapshot, valid_from=date(2027, 2, 28))
    copy_version(db, open_snapshot, valid_from=date(2027, 3, 1))
    assert (
        db.scalar(
            select(func.count())
            .select_from(EmployeeVersion)
            .where(EmployeeVersion.snapshot_id == open_snapshot.id)
        )
        == 2
    )
    db.add(
        OrgVersion(
            snapshot_id=open_snapshot.id,
            org_id="D01",
            name="Synthetic org",
            region="Global",
            valid_from=date(2027, 1, 1),
        )
    )
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            OrgVersion(
                snapshot_id=open_snapshot.id,
                org_id="D01",
                name="Overlap",
                region="Global",
                valid_from=date(2027, 2, 1),
            )
        )
        db.flush()


@pytest.mark.security
def test_secondary_suppression_hides_entire_breakdown(client, login, db, open_snapshot):
    # One small department must not be recoverable as total minus the published large department.
    for i in range(1, 7):
        copy_version(
            db, open_snapshot, employee_id=f"SYN-{i:06d}", org_id="D01" if i <= 5 else "D02"
        )
    open_snapshot.status = "PUBLISHED"
    db.commit()
    login("executive")
    data = get(client, "/api/v1/workforce/summary", snapshot_id=open_snapshot.id).json()
    assert data["headcount"] == 6
    assert data["departments"] == []
    assert data["suppressed"] is True
    assert get(client, "/api/v1/org-units", snapshot_id=open_snapshot.id).json() == []


@pytest.mark.parametrize("currency,exponent", [("USD", 2), ("JPY", 0), ("KWD", 3)])
def test_money_remains_exact_in_json(client, login, db, open_snapshot, currency, exponent):
    copy_version(
        db, open_snapshot, currency=currency, annual_pay_minor=Decimal("99999999999999999999")
    )
    open_snapshot.status = "PUBLISHED"
    db.commit()
    login()
    amount = get(client, "/api/v1/people/SYN-000001", snapshot_id=open_snapshot.id).json()[
        "annual_pay"
    ]
    assert amount == {
        "minor_units": "99999999999999999999",
        "currency": currency,
        "exponent": exponent,
    }


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", " \r=1"])
@pytest.mark.security
def test_export_formula_neutralization(value):
    assert safe_cell(value).startswith("'")


@pytest.mark.parametrize(
    "persona,total,pay",
    [
        ("hr", 5000, True),
        ("finance", 5000, True),
        ("manager-a", 207, True),
        ("manager-b", 625, True),
        ("approver", 415, True),
        ("steward", 5000, False),
    ],
)
def test_persona_read_projection_matrix(client, login, persona, total, pay):
    login(persona)
    data = get(client).json()
    assert data["total"] == total
    assert all(("annual_pay" in row) == pay for row in data["items"])
    assert get(client, "/api/v1/workforce/summary").json()["headcount"] == total
    assert client.get("/api/v1/snapshots").status_code == 200
    assert get(client, "/api/v1/org-units").status_code == 200
    if persona == "approver":
        assert get(client, "/api/v1/people/SYN-000014").status_code == 404
        assert get(client, "/api/v1/people/export").status_code == 403
    if persona == "finance":
        assert (
            len(list(csv.DictReader(io.StringIO(get(client, "/api/v1/people/export").text))))
            == 5000
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/snapshots",
        "/api/v1/people",
        "/api/v1/people/SYN-000001",
        "/api/v1/people/SYN-000001/history",
        "/api/v1/people/export",
        "/api/v1/workforce/summary",
        "/api/v1/org-units",
    ],
)
@pytest.mark.security
def test_every_workforce_route_requires_trusted_session(client, path):
    response = client.get(
        path,
        headers={
            "Authorization": "Bearer client-invented-hr-token",
            "X-Role": "hr",
            "X-Scope": "*",
        },
    )
    assert response.status_code == 401


@pytest.mark.security
def test_auditor_has_metadata_not_workforce_metrics(client, login):
    login("auditor")
    assert client.get("/api/v1/snapshots").status_code == 200
    assert get(client, "/api/v1/workforce/summary").status_code == 403
    assert get(client, "/api/v1/org-units").status_code == 403
