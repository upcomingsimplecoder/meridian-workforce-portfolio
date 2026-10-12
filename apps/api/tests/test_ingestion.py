import hashlib
import json
from datetime import date, timedelta

import pytest
from meridian import object_store
from meridian.ingestion_models import (
    IngestionManifest,
    IngestionRecord,
    SnapshotPublication,
)
from meridian.ingestion_parser import SourceRow, graph_defects, merge_histories, parse_source
from meridian.ingestion_service import base_rows
from meridian.models import AuditEvent, ConsumerReceipt, OutboxEvent, RoleAssignment, now, uid
from meridian.people_models import EmployeeVersion, Snapshot
from meridian.people_seed import CURRENT_SNAPSHOT
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from apps.worker import run as worker
from scripts.generate_workforce_fixtures import csv_bytes, fixture_rows


@pytest.fixture
def ingestion_worker(db, monkeypatch):
    monkeypatch.setattr(
        worker,
        "SessionLocal",
        sessionmaker(
            bind=db.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
    )

    def tick():
        result = worker.tick()
        db.expire_all()
        return result

    return tick


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    # Use the genuine filesystem adapter with isolated private test files.
    from meridian.config import settings

    monkeypatch.setattr(settings(), "local_data_dir", str(tmp_path))
    object_store.ensure_key()
    return tmp_path


def upload(client, data, media="text/csv", **intent_changes):
    body = {
        "media_type": media,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    } | intent_changes
    response = client.post(
        "/api/v1/ingestion/uploads", json=body, headers={"Idempotency-Key": uid()}
    )
    assert response.status_code == 201
    intent = response.json()
    response = client.put(intent["upload_url"], content=data, headers={"Content-Type": media})
    assert response.status_code == 200, response.text
    return intent


def register(client, intent, **changes):
    body = {
        "upload_id": intent["id"],
        "base_snapshot_id": CURRENT_SNAPSHOT,
        "title": "Synthetic July source",
        "watermark": "2027-07-31",
        "schema_version": 1,
    } | changes
    response = client.post(
        "/api/v1/ingestion/batches", json=body, headers={"Idempotency-Key": uid()}
    )
    assert response.status_code == 201, response.text
    return response.json()


def detail(client, batch):
    response = client.get(f"/api/v1/ingestion/batches/{batch['id']}")
    assert response.status_code == 200
    return response.json()


def publish(client, batch, key=None, **body):
    return client.post(
        f"/api/v1/ingestion/batches/{batch['id']}/publish",
        json={"confirm_zero_rejects": True} | body,
        headers={"If-Match": str(batch["version"]), "Idempotency-Key": key or uid()},
    )


def test_5000_rows_quarantine_correction_publication_and_manifest(
    client, login, db, private_store, ingestion_worker
):
    login("steward")
    initial_count = db.scalar(select(func.count()).select_from(EmployeeVersion))
    bad = register(client, upload(client, csv_bytes(fixture_rows(True))))
    assert bad["state"] == "RECEIVED" and bad["pending_job"]
    assert ingestion_worker() == 1
    bad = detail(client, bad)
    assert (bad["accepted_count"], bad["rejected_count"], bad["total_count"]) == (4960, 40, 5000)
    assert bad["state"] == "QUARANTINED"
    errors = client.get(f"/api/v1/ingestion/batches/{bad['id']}/errors").json()
    assert errors["total"] == 40 and len(errors["items"]) == 40
    assert {row["code"] for row in errors["items"]} == {
        "INVALID_FIELD",
        "UNKNOWN_ORG",
        "INVALID_REPORTING_GRAPH",
    }
    assert publish(client, bad).status_code == 409
    assert db.scalar(select(func.count()).select_from(EmployeeVersion)) == initial_count
    preview = client.get(f"/api/v1/ingestion/batches/{bad['id']}/preview")
    assert preview.status_code == 200
    assert len(preview.json()["items"]) == 25
    assert "annual_pay" not in preview.text and "currency" not in preview.text
    fixed = register(
        client, upload(client, csv_bytes(fixture_rows())), supersedes_batch_id=bad["id"]
    )
    assert ingestion_worker() == 1
    fixed = detail(client, fixed)
    assert fixed["state"] == "VALIDATED"
    assert (fixed["accepted_count"], fixed["rejected_count"], fixed["total_count"]) == (
        5000,
        0,
        5000,
    )
    key = uid()
    response = publish(client, fixed, key)
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["state"] == "PUBLISHED" and done["snapshot_id"]
    assert done["pending_job"] and not done["manifest_ready"]
    assert publish(client, fixed, key).json()["snapshot_id"] == done["snapshot_id"]
    assert publish(client, fixed).status_code == 409
    assert ingestion_worker() == 1
    done = detail(client, done)
    assert done["manifest_ready"] and not done["pending_job"]
    assert (
        db.scalar(
            select(func.count()).select_from(Snapshot).where(Snapshot.source_batch_id == done["id"])
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.verb == "source_batch.published")
        )
        == 1
    )
    assert db.scalar(select(func.count()).select_from(IngestionManifest)) == 1
    assert db.scalar(select(func.count()).select_from(SnapshotPublication)) == 3
    # Actual publication order wins over fictional seed capture dates, while old facts remain pinned.
    assert client.get("/api/v1/snapshots").json()[0]["id"] == done["snapshot_id"]
    june = client.get(
        "/api/v1/people/SYN-000037",
        params={"snapshot_id": done["snapshot_id"], "as_of": "2027-06-30"},
    ).json()
    july = client.get(
        "/api/v1/people/SYN-000037",
        params={"snapshot_id": done["snapshot_id"], "as_of": "2027-07-01"},
    ).json()
    assert june["org_id"] == "D02" and july["org_id"] == "D01"
    assert june["source_batch_id"] != done["id"] and july["source_batch_id"] == done["id"]
    metrics = client.get(
        "/api/v1/workforce/summary",
        params={"snapshot_id": done["snapshot_id"], "as_of": "2027-07-31"},
    ).json()
    assert (metrics["headcount"], metrics["fte"]) == (5000, "4900.0000")
    manifest = db.get(IngestionManifest, done["id"])
    assert (
        hashlib.sha256(object_store.read_private(manifest.storage_key)).hexdigest()
        == manifest.sha256
    )
    assert (
        manifest.document["accepted"],
        manifest.document["rejected"],
        manifest.document["total"],
    ) == (5000, 0, 5000)
    assert manifest.document["employee_versions"] == 10002
    assert all(path.stat().st_mode & 0o077 == 0 for path in (private_store / "objects").iterdir())


def test_duplicate_file_and_worker_delivery_are_noops(
    client, login, db, private_store, ingestion_worker
):
    login("hr")
    data = csv_bytes(fixture_rows()[:1])
    one = register(client, upload(client, data))
    two = register(client, upload(client, data))
    assert one["id"] == two["id"]
    assert ingestion_worker() == 1
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == one["id"]))
    event.state = "PENDING"
    db.commit()
    assert ingestion_worker() == 1
    assert db.scalar(select(func.count()).select_from(IngestionRecord)) == 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(ConsumerReceipt)
            .where(ConsumerReceipt.consumer == "ingestion-v1")
        )
        == 1
    )


@pytest.mark.security
@pytest.mark.parametrize(
    "persona", ["manager-a", "manager-b", "finance", "executive", "auditor", "approver"]
)
def test_ingestion_requires_steward_capability(client, login, persona):
    login(persona)
    assert client.get("/api/v1/ingestion/batches").status_code == 403
    body = {"media_type": "text/csv", "size_bytes": 1, "sha256": "0" * 64}
    assert (
        client.post(
            "/api/v1/ingestion/uploads", json=body, headers={"Idempotency-Key": uid()}
        ).status_code
        == 403
    )
    for suffix in ["", "/preview", "/errors", "/downloads/errors?token=fake"]:
        assert client.get(f"/api/v1/ingestion/batches/{uid()}" + suffix).status_code == 403


@pytest.mark.security
@pytest.mark.parametrize(
    "defect", ["checksum", "content-type", "oversize", "token", "principal", "csrf"]
)
def test_signed_upload_enforces_binding_limits_and_csrf(client, login, db, private_store, defect):
    login("steward")
    data = b"a,b\n"
    body = {
        "media_type": "text/csv",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    intent = client.post(
        "/api/v1/ingestion/uploads", json=body, headers={"Idempotency-Key": uid()}
    ).json()
    target = intent["upload_url"]
    content = data
    headers = {"Content-Type": "text/csv"}
    if defect == "checksum":
        content = b"a,x\n"
    if defect == "content-type":
        headers["Content-Type"] = "text/html"
    if defect == "oversize":
        content = data + b"x"
    if defect == "token":
        target = target.split("?token=")[0] + "?token=forged"
    if defect == "principal":
        login("hr")
    if defect == "csrf":
        headers["X-CSRF-Token"] = "wrong"
    response = client.put(target, content=content, headers=headers)
    assert response.status_code in {403, 404, 413, 415, 422}
    assert (
        not list((private_store / "objects").glob("*"))
        if (private_store / "objects").exists()
        else True
    )


@pytest.mark.security
def test_idempotency_key_reuse_and_unknown_fields_cannot_mutate_intents(
    client, login, private_store
):
    login("steward")
    key = uid()
    body = {"media_type": "text/csv", "size_bytes": 1, "sha256": "0" * 64}
    first = client.post("/api/v1/ingestion/uploads", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    same = client.post("/api/v1/ingestion/uploads", json=body, headers={"Idempotency-Key": key})
    assert first.json() == same.json()
    assert (
        client.post(
            "/api/v1/ingestion/uploads",
            json=body | {"size_bytes": 2},
            headers={"Idempotency-Key": key},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/ingestion/uploads",
            json=body | {"actor_id": "hr"},
            headers={"Idempotency-Key": uid()},
        ).status_code
        == 422
    )


@pytest.mark.security
def test_signed_errors_recheck_scope_session_expiry_and_revision(
    client, login, db, private_store, ingestion_worker
):
    login("steward")
    batch = register(client, upload(client, b"invalid,header\nprivate-rejected-value,=cmd\n"))
    assert ingestion_worker() == 1
    batch = detail(client, batch)
    errors = client.get(f"/api/v1/ingestion/batches/{batch['id']}/errors").json()
    assert errors["items"][0]["row_number"] == 0
    path = f"/api/v1/ingestion/batches/{batch['id']}/download-links"
    result = client.post(path, json={"kind": "errors"}, headers={"Idempotency-Key": uid()})
    assert result.status_code == 200
    link = result.json()["url"]
    response = client.get(link)
    assert response.status_code == 200 and "INVALID_FILE" in response.text
    assert "private-rejected-value" not in response.text and "=cmd" not in response.text
    expired = object_store.sign_link("steward", "errors", batch["id"], batch["version"], seconds=-1)
    assert client.get(link.split("?token=")[0] + "?token=" + expired).status_code == 403
    stale = object_store.sign_link("steward", "errors", batch["id"], batch["version"] - 1)
    assert client.get(link.split("?token=")[0] + "?token=" + stale).status_code == 403
    login("executive")
    assert client.get(link).status_code == 403
    login("hr")
    assert client.get(link).status_code == 403
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
    assert client.get(f"/api/v1/ingestion/batches/{batch['id']}").status_code == 404
    assert client.get(link).status_code == 404


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    [
        {"manager_id": "SYN-000001"},
        {"manager_id": "SYN-999999"},
        {"valid_to": "2027-01-01"},
        {"currency": "XXX"},
        {"annual_pay_minor": "1.5"},
        {"annual_pay_minor": "1" * 21},
        {"display_name": "Actual Person"},
        {"fte": "0.0000"},
        {"org_id": "D99"},
        {"display_name": "=HYPERLINK(unsafe)"},
        {"valid_from": "2028-01-01"},
    ],
)
def test_parser_rejects_invalid_rows_without_echoing_values(db, change):
    rows = fixture_rows()[:1]
    rows[0].update(change)
    parsed = parse_source(
        csv_bytes(rows),
        "text/csv",
        date(2027, 7, 31),
        base_rows(db, CURRENT_SNAPSHOT),
        {f"D{i:02d}" for i in range(1, 13)},
    )
    assert parsed.total == 1 and not parsed.records and parsed.errors
    assert "Actual Person" not in str(parsed.errors)
    assert "HYPERLINK" not in str(parsed.errors)


def test_json_contract_and_duplicate_intervals(db):
    rows = fixture_rows()[:1]
    orgs = {"D01"}
    parsed = parse_source(
        json.dumps({"schema_version": 1, "records": rows}).encode(),
        "application/json",
        date(2027, 7, 31),
        base_rows(db, CURRENT_SNAPSHOT),
        orgs,
    )
    assert len(parsed.records) == 1
    duplicated = parse_source(
        csv_bytes(rows + rows), "text/csv", date(2027, 7, 31), base_rows(db, CURRENT_SNAPSHOT), orgs
    )
    assert duplicated.total == 2 and not duplicated.records
    assert {error.code for error in duplicated.errors} == {"OVERLAPPING_VERSION"}


def test_interval_replacement_splits_and_preserves_unaffected_history():
    base = SourceRow.model_validate(
        fixture_rows()[0] | {"manager_id": None, "valid_from": "2027-01-01", "valid_to": None}
    ).model_dump(mode="json")
    correction = base | {
        "valid_from": "2027-03-01",
        "valid_to": "2027-05-01",
        "annual_pay_minor": "123",
    }
    merged = merge_histories([base], [correction])
    assert [(r["valid_from"], r["valid_to"]) for r in merged] == [
        ("2027-01-01", "2027-03-01"),
        ("2027-03-01", "2027-05-01"),
        ("2027-05-01", None),
    ]
    assert (
        merged[0]["annual_pay_minor"] == base["annual_pay_minor"] == merged[2]["annual_pay_minor"]
    )
    assert merged[1]["annual_pay_minor"] == "123"
    assert graph_defects(merged) == set()


class WorkerCrash(BaseException):
    """An abrupt process exit, outside the worker's recoverable exception boundary."""


def test_worker_crash_after_partial_validation_rolls_back_and_resumes_once(
    client, login, db, private_store, ingestion_worker, monkeypatch
):
    from meridian.ingestion_models import IngestionInput
    from meridian.ingestion_service import validate_batch
    from meridian.people_models import SourceBatch

    login("steward")
    batch = register(client, upload(client, csv_bytes(fixture_rows()[:2])))

    def crash(session, batch_id):
        # Actually perform the writes, then terminate before receipt or commit.
        validate_batch(session, batch_id)
        session.flush()
        assert session.scalar(select(func.count()).select_from(IngestionRecord)) == 2
        raise WorkerCrash()

    monkeypatch.setattr(worker, "validate_batch", crash)
    with pytest.raises(WorkerCrash):
        ingestion_worker()
    db.expire_all()
    assert db.get(SourceBatch, batch["id"]).state == "RECEIVED"
    assert db.get(IngestionInput, batch["id"]).version == 1
    assert db.scalar(select(func.count()).select_from(IngestionRecord)) == 0
    assert db.scalar(select(func.count()).select_from(ConsumerReceipt)) == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.verb == "source_batch.validated")
        )
        == 0
    )
    monkeypatch.setattr(worker, "validate_batch", validate_batch)
    assert ingestion_worker() == 1
    assert detail(client, batch)["accepted_count"] == 2
    assert db.scalar(select(func.count()).select_from(IngestionRecord)) == 2


def test_storage_outage_dead_letter_explicit_retry_and_reason_are_durable(
    client, login, db, private_store, ingestion_worker, monkeypatch
):
    from meridian.ingestion_models import IdempotencyReceipt

    login("steward")
    batch = register(client, upload(client, csv_bytes(fixture_rows()[:1])))
    read = object_store.read_private

    def unavailable(key):
        raise OSError("sensitive-provider-details-must-not-be-logged")

    monkeypatch.setattr(object_store, "read_private", unavailable)
    for attempt in range(5):
        assert ingestion_worker() == 0
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == batch["id"]))
        assert event.attempts == attempt + 1
        assert event.last_error == "JOB_EXECUTION_FAILED"
        event.next_attempt_at = now() - timedelta(seconds=1)
        db.commit()
    dead = detail(client, batch)
    assert dead["failed_job"] and not dead["pending_job"] and dead["state"] == "RECEIVED"
    assert not db.scalar(select(func.count()).select_from(IngestionRecord))
    assert publish(client, dead).status_code == 409
    monkeypatch.setattr(object_store, "read_private", read)
    response = client.post(
        f"/api/v1/ingestion/batches/{batch['id']}/retry",
        json={"reason": "Restored private source storage availability"},
        headers={"If-Match": str(dead["version"]), "Idempotency-Key": uid()},
    )
    assert response.status_code == 200
    assert response.json()["pending_job"] and response.json()["version"] == 2
    receipt = db.scalar(
        select(IdempotencyReceipt).where(IdempotencyReceipt.route.endswith("/retry"))
    )
    assert receipt.request_payload["reason"] == "Restored private source storage availability"
    audit = db.scalar(select(AuditEvent).where(AuditEvent.verb == "source_batch.retried"))
    assert audit.request_id == receipt.request_id and audit.entity_version == 2
    assert ingestion_worker() == 1
    assert detail(client, batch)["state"] == "VALIDATED"


def test_manifest_crash_after_object_write_is_safe_to_repeat(
    client, login, db, private_store, ingestion_worker, monkeypatch
):
    from meridian.ingestion_service import write_manifest

    login("hr")
    batch = register(client, upload(client, csv_bytes(fixture_rows()[:1])))
    assert ingestion_worker() == 1
    batch = detail(client, batch)
    assert publish(client, batch).status_code == 200

    def crash(session, batch_id):
        write_manifest(session, batch_id)
        session.flush()
        raise WorkerCrash()

    monkeypatch.setattr(worker, "write_manifest", crash)
    with pytest.raises(WorkerCrash):
        ingestion_worker()
    db.expire_all()
    objects_before = set((private_store / "objects").iterdir())
    assert len(objects_before) == 2
    assert db.get(IngestionManifest, batch["id"]) is None
    assert not detail(client, batch)["manifest_ready"]
    monkeypatch.setattr(worker, "write_manifest", write_manifest)
    assert ingestion_worker() == 1
    assert detail(client, batch)["manifest_ready"]
    assert set((private_store / "objects").iterdir()) == objects_before
    event = db.scalar(
        select(OutboxEvent).where(OutboxEvent.event_type == "source_batch.published.v1")
    )
    event.state = "PENDING"
    db.commit()
    assert ingestion_worker() == 1
    assert db.scalar(select(func.count()).select_from(IngestionManifest)) == 1
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 3


def test_failed_publication_rolls_back_versions_snapshot_audit_and_outbox(
    client, login, db, private_store, ingestion_worker, monkeypatch
):
    from meridian import ingestion_service
    from meridian.ingestion_models import IngestionInput
    from meridian.people_models import SourceBatch

    login("steward")
    batch = register(client, upload(client, csv_bytes(fixture_rows()[:1])))
    assert ingestion_worker() == 1
    source = db.get(IngestionInput, batch["id"])
    original_versions = db.scalar(select(func.count()).select_from(EmployeeVersion))
    original_audit = db.scalar(select(func.count()).select_from(AuditEvent))
    original_events = db.scalar(select(func.count()).select_from(OutboxEvent))
    original_record = ingestion_service.record

    def fail_after_record(*args, **kwargs):
        original_record(*args, **kwargs)
        args[0].flush()
        raise RuntimeError("simulated commit boundary failure")

    monkeypatch.setattr(ingestion_service, "record", fail_after_record)
    with pytest.raises(RuntimeError), db.begin_nested():
        ingestion_service.publish_batch(
            db, db.get(SourceBatch, batch["id"]), source, "steward", uid()
        )
    assert db.scalar(select(func.count()).select_from(EmployeeVersion)) == original_versions
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == original_audit
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == original_events
    assert db.get(SourceBatch, batch["id"]).state == "VALIDATED"
    monkeypatch.setattr(ingestion_service, "record", original_record)
    assert publish(client, detail(client, batch)).status_code == 200


@pytest.mark.security
@pytest.mark.parametrize(
    "mutation",
    [
        "record_update",
        "record_insert",
        "error_insert",
        "input_pin",
        "source_hash",
        "source_counts",
        "upload_hash",
        "receipt_change",
    ],
)
def test_validated_input_and_provenance_are_sealed_in_postgres(
    client, login, db, private_store, ingestion_worker, mutation
):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    login("steward")
    intent = upload(client, csv_bytes(fixture_rows()[:1]))
    batch = register(client, intent)
    assert ingestion_worker() == 1
    statements = {
        "record_update": "UPDATE ingestion_record SET canonical='{}' WHERE batch_id=:batch",
        "record_insert": "INSERT INTO ingestion_record(batch_id,row_number,canonical) VALUES (:batch,2,'{}')",
        "error_insert": "INSERT INTO ingestion_error(id,batch_id,row_number,field,code,redacted_summary) VALUES (:id,:batch,2,'row','LATE','late error')",
        "input_pin": "UPDATE ingestion_input SET base_snapshot_id=:id,version=version+1 WHERE batch_id=:batch",
        "source_hash": "UPDATE source_batch SET sha256=repeat('0',64) WHERE id=:batch",
        "source_counts": "UPDATE source_batch SET accepted_count=999 WHERE id=:batch",
        "upload_hash": "UPDATE upload_intent SET declared_sha256=repeat('0',64) WHERE id=:upload",
        "receipt_change": "UPDATE idempotency_receipt SET response='{}' WHERE actor_id='steward'",
    }
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(statements[mutation]), {"batch": batch["id"], "upload": intent["id"], "id": uid()}
        )


@pytest.mark.security
@pytest.mark.parametrize("suffix", ["", "/errors", "/preview"])
def test_list_query_overposting_is_rejected(client, login, private_store, suffix):
    login("steward")
    path = "/api/v1/ingestion/batches" + (f"/{uid()}{suffix}" if suffix else "")
    assert client.get(path + "?role=hr&scope=*").status_code == 422


@pytest.mark.security
@pytest.mark.parametrize("mode", ["expired", "revoked", "no_session", "revoked_grant"])
def test_download_links_do_not_bypass_current_access(
    client, login, db, private_store, ingestion_worker, mode
):
    session = login("steward")
    batch = register(client, upload(client, b"wrong-schema\n"))
    assert ingestion_worker() == 1
    response = client.post(
        f"/api/v1/ingestion/batches/{batch['id']}/download-links",
        json={"kind": "errors"},
        headers={"Idempotency-Key": uid()},
    )
    assert response.status_code == 200
    if mode == "expired":
        session.expires_at = now() - timedelta(seconds=1)
    elif mode == "revoked":
        session.revoked = True
    elif mode == "no_session":
        client.cookies.clear()
    else:
        grant = db.scalar(select(RoleAssignment).where(RoleAssignment.principal_id == "steward"))
        grant.effective_to = now() - timedelta(seconds=1)
    db.commit()
    assert client.get(response.json()["url"]).status_code == (
        403 if mode == "revoked_grant" else 401
    )


@pytest.mark.security
@pytest.mark.parametrize(
    "document",
    [
        b"\xff\xfe",
        b"\x00",
        b"",
        b"employee_id,employee_id\na,b\n",
        b'{"schema_version":true,"records":[]}',
        b'{"schema_version":2,"records":[]}',
        b'{"schema_version":1,"records":[],"role":"hr"}',
    ],
)
def test_malformed_sources_are_batch_errors_with_no_echo(document):
    media = "application/json" if document.startswith(b"{") else "text/csv"
    result = parse_source(document, media, date(2027, 7, 31), [], {"D01"})
    assert result.total == 0 and not result.records
    assert result.errors[0].row_number == 0 and result.errors[0].code == "INVALID_FILE"


def test_reporting_graph_checks_every_boundary_and_descendant():
    root = SourceRow.model_validate(
        fixture_rows()[0] | {"manager_id": None, "valid_to": None}
    ).model_dump(mode="json")
    cycle = [
        root | {"employee_id": "SYN-000001", "manager_id": "SYN-000002"},
        root | {"employee_id": "SYN-000002", "manager_id": "SYN-000001"},
        root | {"employee_id": "SYN-000003", "manager_id": "SYN-000002"},
    ]
    assert graph_defects(cycle) == {"SYN-000001", "SYN-000002", "SYN-000003"}
    histories = [
        root | {"valid_to": "2027-08-01"},
        root | {"valid_from": "2027-08-01", "status": "EXITED"},
        root | {"employee_id": "SYN-000002", "manager_id": "SYN-000001"},
    ]
    assert graph_defects(histories) == {"SYN-000002"}


def test_org_requires_continuous_effective_coverage():
    row = fixture_rows()[0] | {"manager_id": None, "valid_to": "2027-10-01"}
    intervals = {"D01": [("2027-01-01", "2027-08-01"), ("2027-09-01", None)]}
    parsed = parse_source(csv_bytes([row]), "text/csv", date(2027, 7, 31), [], {"D01"}, intervals)
    assert not parsed.records and parsed.errors[0].code == "INACTIVE_ORG"
    intervals["D01"][1] = ("2027-08-01", None)
    assert (
        len(
            parse_source(
                csv_bytes([row]), "text/csv", date(2027, 7, 31), [], {"D01"}, intervals
            ).records
        )
        == 1
    )


@pytest.mark.parametrize(
    "currency,amount",
    [("JPY", "99999999999999999999"), ("USD", "12345678901234567890"), ("KWD", "999001")],
)
def test_import_preserves_exact_original_currency_minor_units(db, currency, amount):
    row = fixture_rows()[0] | {"currency": currency, "annual_pay_minor": amount}
    parsed = parse_source(
        csv_bytes([row]), "text/csv", date(2027, 7, 31), base_rows(db, CURRENT_SNAPSHOT), {"D01"}
    )
    assert not parsed.errors
    assert parsed.records[0][1]["annual_pay_minor"] == amount
    assert parsed.records[0][1]["currency"] == currency


def test_manager_exit_quarantines_the_changed_manager_not_unrelated_input(db):
    base = base_rows(db, CURRENT_SNAPSHOT)
    unchanged_leaf = fixture_rows()[100]
    exited_manager = fixture_rows()[0] | {"status": "EXITED"}
    parsed = parse_source(
        csv_bytes([unchanged_leaf, exited_manager]),
        "text/csv",
        date(2027, 7, 31),
        base,
        {f"D{i:02d}" for i in range(1, 13)},
    )
    assert [(error.row_number, error.code) for error in parsed.errors] == [
        (2, "INVALID_REPORTING_GRAPH")
    ]
    assert [number for number, _ in parsed.records] == [1]


def test_batch_register_pages_are_stable_newest_first(client, login, private_store):
    login("steward")
    registered = []
    for count in range(1, 5):
        batch = register(client, upload(client, csv_bytes(fixture_rows()[:count])))
        registered.append(batch["id"])
    first = client.get("/api/v1/ingestion/batches?limit=2").json()
    second = client.get(
        "/api/v1/ingestion/batches", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert [row["id"] for row in first["items"] + second["items"]] == list(reversed(registered))
    assert second["next_cursor"] is None


@pytest.mark.security
@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_registration_requires_integer_schema_version(client, login, version):
    login("steward")
    result = client.post(
        "/api/v1/ingestion/batches",
        headers={"Idempotency-Key": uid()},
        json={
            "upload_id": uid(),
            "base_snapshot_id": CURRENT_SNAPSHOT,
            "watermark": "2027-07-31",
            "title": "Schema test",
            "schema_version": version,
        },
    )
    assert result.status_code == 422


@pytest.mark.security
@pytest.mark.parametrize(
    "document",
    [
        b'{"schema_version":2,"schema_version":1,"records":[]}',
        b'{"schema_version":1,"records":[{"employee_id":"SYN-000002","employee_id":"SYN-000001"}]}',
    ],
)
def test_json_duplicate_keys_are_not_silently_overwritten(document):
    parsed = parse_source(document, "application/json", date(2027, 7, 31), [], {"D01"})
    assert not parsed.records and parsed.errors[0].code == "INVALID_FILE"


@pytest.mark.security
@pytest.mark.parametrize("value", [0, 1798761600, "2027-01-01T00:00:00Z", True])
def test_source_dates_do_not_coerce_timestamps(value):
    document = {
        "schema_version": 1,
        "records": [fixture_rows()[0] | {"valid_from": value, "manager_id": None}],
    }
    parsed = parse_source(
        json.dumps(document).encode(), "application/json", date(2027, 7, 31), [], {"D01"}
    )
    assert not parsed.records and parsed.errors[0].code == "INVALID_FIELD"


@pytest.mark.security
@pytest.mark.parametrize("source", [None, "different", "same"])
def test_source_event_envelope_requires_bound_source_provenance(source):
    from meridian.schemas import EventEnvelope
    from pydantic import ValidationError

    batch_id = uid()
    envelope = {
        "event_id": uid(),
        "event_type": "source_batch.published.v1",
        "schema_version": 1,
        "aggregate_id": batch_id,
        "aggregate_version": 3,
        "occurred_at": now(),
        "correlation_id": uid(),
        "payload": {"entity_type": "source_batch", "entity_id": batch_id},
        "source_batch_id": batch_id
        if source == "same"
        else uid()
        if source == "different"
        else None,
    }
    if source == "same":
        assert str(EventEnvelope.model_validate(envelope).source_batch_id) == batch_id
    else:
        with pytest.raises(ValidationError):
            EventEnvelope.model_validate(envelope)
