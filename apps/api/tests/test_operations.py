"""Actual HTTP, SDK, SQL correlation, privacy and operational authorization."""

from datetime import timedelta
from decimal import Decimal

import pytest
from meridian import telemetry
from meridian.models import ConsumerReceipt, OutboxEvent, WorkerHeartbeat, now, uid
from meridian.operations import percentile
from meridian.operations_models import OutboxTrace, TraceSpan
from meridian.people_models import AuthzPolicy
from meridian.policy import POLICY_V4_DEFINITION, canonical_hash
from opentelemetry.trace import SpanKind
from sqlalchemy import insert, select, update
from sqlalchemy.orm import sessionmaker
from test_compensation_api import comp_worker as comp_worker

from apps.worker import run as worker


@pytest.mark.security
@pytest.mark.parametrize("persona", ["manager-a", "manager-b", "approver", "executive", "steward"])
def test_operations_and_trace_require_global_explicit_grants(client, login, persona):
    login(persona)
    for path in ("/api/v1/operations", "/api/v1/operations/traces/" + "a" * 32):
        response = client.get(path)
        assert response.status_code == 403
        assert "recent_spans" not in response.text and "request_id" not in response.json().get(
            "detail", ""
        )


@pytest.mark.parametrize("persona", ["hr", "finance", "auditor"])
def test_operations_reports_actual_database_observations(client, login, db, persona):
    login(persona)
    measured = now()
    heartbeat = db.get(WorkerHeartbeat, "outbox")
    if heartbeat:
        heartbeat.last_seen_at = measured - timedelta(seconds=180)
    else:
        db.add(WorkerHeartbeat(name="outbox", last_seen_at=measured - timedelta(seconds=180)))
    db.add(
        OutboxEvent(
            event_type="identity.logout.v1",
            aggregate_id="hr",
            request_id=uid(),
            payload={"entity_type": "principal", "entity_id": "hr"},
            occurred_at=measured - timedelta(seconds=70),
        )
    )
    db.commit()
    response = client.get("/api/v1/operations")
    assert response.status_code == 200
    data = response.json()
    assert data["pending_jobs"] >= 1 and data["oldest_pending_seconds"] >= 70
    assert {"worker_outbox_stale", "job_lag"} <= {a["code"] for a in data["alerts"]}
    assert next(w for w in data["workers"] if w["name"] == "outbox")["state"] == "STALE"
    assert data["sample_limit"] == 5000 and len(data["recent_spans"]) <= 50


def test_real_sdk_api_event_worker_parent_and_receipt_survive_commit(
    client, login, db, monkeypatch
):
    login("manager-a")
    forged = "00-" + "f" * 32 + "-" + "f" * 16 + "-01"
    response = client.post(
        "/api/v1/logout", headers={"traceparent": forged, "baggage": "salary=DO_NOT_EXPORT_PRIVATE"}
    )
    assert response.status_code == 204
    request_id = response.headers["x-request-id"]
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.request_id == request_id))
    origin = db.get(OutboxTrace, event.id)
    assert origin and origin.trace_id != "f" * 32
    root = db.get(TraceSpan, origin.parent_span_id)
    assert root and root.service == "meridian-api" and root.http_status == 204
    assert root.route == "/api/v1/logout" and root.parent_span_id is None
    db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id != event.id)
        .values(next_attempt_at=now() + timedelta(days=1))
    )
    db.commit()
    factory = sessionmaker(
        bind=db.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setitem(telemetry._factories, "meridian-worker", factory)
    assert worker.tick() == 1
    db.expire_all()
    assert db.get(ConsumerReceipt, ("foundation", event.id))
    consumer = db.scalar(select(TraceSpan).where(TraceSpan.event_id == event.id))
    assert (
        consumer
        and consumer.parent_span_id == root.span_id
        and consumer.trace_id == origin.trace_id
    )
    assert consumer.service == "meridian-worker" and consumer.outcome == "OK"
    login("auditor")
    result = client.get("/api/v1/operations/traces/" + origin.trace_id)
    assert result.status_code == 200
    data = result.json()
    assert len(data["spans"]) == 2 and not data["truncated"]
    assert "DO_NOT_EXPORT_PRIVATE" not in result.text and "manager-a" not in result.text
    assert all("payload" not in span and "headers" not in span for span in data["spans"])


@pytest.mark.security
def test_sdk_export_projects_only_safe_attributes_no_exception_or_payload(client, login, db):
    login()
    request_id = uid()
    with telemetry.tracer("meridian-api").start_as_current_span(
        "PRIVATE_TITLE_MUST_NOT_EXPORT",
        kind=SpanKind.SERVER,
        attributes={
            "meridian.request_id": request_id,
            "http.route": "/private/PERSON_NAME",
            "http.request.header.authorization": "SECRET_BEARER",
            "pay_minor": 123456789,
            "messaging.event_id": "PERSON_NAME",
            "http.response.status_code": 200,
        },
    ) as span:
        span.add_event("PRIVATE_NARRATIVE", {"cookie": "SECRET_COOKIE"})
    row = db.scalar(select(TraceSpan).where(TraceSpan.request_id == request_id))
    assert row and row.route == "[unmatched]" and row.event_id is None
    response = client.get("/api/v1/operations/traces/" + row.trace_id)
    assert response.status_code == 200
    for sensitive in (
        "PRIVATE_TITLE",
        "PERSON_NAME",
        "SECRET_BEARER",
        "123456789",
        "PRIVATE_NARRATIVE",
        "SECRET_COOKIE",
    ):
        assert sensitive not in response.text


def test_telemetry_export_outage_does_not_reverse_acknowledged_command(
    client, login, db, monkeypatch, caplog
):
    login()

    def unavailable():
        raise RuntimeError("PRIVATE_CONNECTION_DETAIL")

    monkeypatch.setitem(telemetry._factories, "meridian-api", unavailable)
    response = client.post("/api/v1/logout")
    assert response.status_code == 204
    event = db.scalar(
        select(OutboxEvent).where(OutboxEvent.request_id == response.headers["x-request-id"])
    )
    assert event and db.get(OutboxTrace, event.id)
    assert "telemetry_export_unavailable" in caplog.text
    assert "PRIVATE_CONNECTION_DETAIL" not in caplog.text


def test_historical_v4_policy_stays_byte_exact(db):
    assert db.get(AuthzPolicy, "authz-v4").sha256 == canonical_hash(POLICY_V4_DEFINITION)
    assert "operations.read" not in POLICY_V4_DEFINITION["hr"]


def test_nearest_rank_percentiles_use_exact_observed_decimal_values():
    assert percentile([], 95) is None
    assert percentile([Decimal("1.001"), Decimal("1.002"), Decimal("100.999")], 95) == Decimal(
        "100.999"
    )


@pytest.mark.security
@pytest.mark.parametrize(
    "trace_id",
    ["A" * 32, "not-a-trace", "a" * 31, str(uid())],
    ids=["uppercase", "non-hex", "short", "uuid-not-trace"],
)
def test_trace_identifier_validation_does_not_query_unbounded_values(client, login, trace_id):
    login("auditor")
    assert client.get("/api/v1/operations/traces/" + trace_id).status_code == 422


def test_database_window_and_trace_response_are_bounded_and_label_truncation(client, login, db):
    login("auditor")
    measured = now() - timedelta(seconds=5)
    trace_id, request_id = "e" * 32, uid()
    db.execute(
        insert(TraceSpan),
        [
            dict(
                span_id=f"{i:016x}",
                trace_id=trace_id,
                request_id=request_id,
                service="meridian-api",
                kind="SERVER",
                outcome="OK",
                started_at=measured,
                duration_ms=Decimal("1.000"),
                http_status=200,
                route="/health",
            )
            for i in range(1, 5002)
        ],
    )
    db.commit()
    data = client.get("/api/v1/operations").json()
    assert data["sample_truncated"] and len(data["recent_spans"]) == 50
    assert data["http_requests"] == 5000
    trace = client.get("/api/v1/operations/traces/" + trace_id)
    assert trace.status_code == 200
    assert trace.json()["truncated"] and len(trace.json()["spans"]) == 200
    assert len(trace.content) < 150000


def test_financial_award_verification_correlates_without_compensation_fields(
    client, login, db, comp_worker
):
    from test_compensation_api import award, launched, roster

    cycle = launched(client, login)
    proposal = next(p for p in roster(client, cycle) if p["employee_id"] == "SYN-000025")
    login("manager-a")
    response = award(client, proposal)
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.request_id == request_id))
    origin = db.get(OutboxTrace, event.id)
    assert origin
    for _ in range(20):
        if db.get(ConsumerReceipt, ("compensation-v1", event.id)):
            break
        assert comp_worker() == 1
    else:
        raise AssertionError("Financial verification was not acknowledged")
    child = db.scalar(select(TraceSpan).where(TraceSpan.event_id == event.id))
    assert child and child.outcome == "OK" and child.parent_span_id == origin.parent_span_id
    assert (
        db.get(TraceSpan, origin.parent_span_id).route
        == "/api/v1/compensation/proposals/{proposal_id}/award"
    )
    login("auditor")
    trace = client.get("/api/v1/operations/traces/" + origin.trace_id)
    assert trace.status_code == 200
    for forbidden in (
        "SYN-",
        "merit_minor",
        "bonus_minor",
        "Synthetic proposal rationale",
        "manager-a",
    ):
        assert forbidden not in trace.text
