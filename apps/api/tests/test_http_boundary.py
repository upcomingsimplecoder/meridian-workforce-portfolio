import asyncio
import json
import logging
from collections import deque
from io import StringIO

import httpx
import pytest
from meridian import http_boundary as boundary
from meridian.db import get_db
from meridian.main import app
from meridian.models import AuditEvent, OutboxEvent
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError


@pytest.mark.security
def test_chunked_body_limit_precedes_logout_and_keeps_session(client, login, db):
    session = login()
    before = db.scalar(select(func.count()).select_from(OutboxEvent))
    response = client.post("/api/v1/logout", content=(b"x" * (1024 * 1024) for _ in range(9)))
    assert response.status_code == 413
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    db.refresh(session)
    assert not session.revoked
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == before
    assert client.get("/api/v1/me").status_code == 200


@pytest.mark.security
@pytest.mark.parametrize(
    "headers,content",
    [
        ({"content-length": "0"}, b"nonempty"),
        ({"content-length": "-1"}, b""),
        ({"content-length": "invalid"}, b""),
        ({"content-length": "0", "transfer-encoding": "chunked"}, b""),
    ],
)
def test_false_or_ambiguous_lengths_cannot_mutate(client, login, db, headers, content):
    session = login()
    response = client.post("/api/v1/logout", content=content, headers=headers)
    assert response.status_code == 400
    db.refresh(session)
    assert not session.revoked
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.security
def test_host_target_and_validation_values_fail_closed(client, login):
    login()
    host = client.get("/api/v1/me", headers={"host": "attacker.example"})
    assert host.status_code == 400 and host.json()["title"] == "Untrusted Host"
    oversized = client.get("/api/v1/me?" + "private-input" * 800)
    assert oversized.status_code == 414
    assert len(oversized.content) < 1000
    assert oversized.headers["cache-control"] == "no-store"
    invalid = client.post("/api/v1/replay/capsules", json={"password": "sensitive-test-sentinel"})
    assert invalid.status_code == 422 and "sensitive-test-sentinel" not in invalid.text


@pytest.mark.security
@pytest.mark.parametrize("host", ["attacker@127.0.0.1:3017", "127.0.0.1/path", "localhost#x"])
def test_invalid_host_authority_cannot_reach_authenticated_routes(client, login, host):
    login()
    response = client.get("/api/v1/me", headers={"host": host})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.security
def test_long_denial_is_durable_fingerprinted_and_not_a_500(client, login, db):
    login("executive")
    value = "SYN-private-path-sentinel-" * 10
    snapshot = "28f7ae3e-f62b-5dbf-98a8-3a6ade9fea37"
    emitted = StringIO()
    handler = logging.StreamHandler(emitted)
    boundary.logger.addHandler(handler)
    try:
        response = client.get(
            "/api/v1/people/" + value + "?as_of=2027-06-30&snapshot_id=" + snapshot
        )
    finally:
        boundary.logger.removeHandler(handler)
    assert response.status_code == 403
    event = db.scalar(select(AuditEvent).where(AuditEvent.permission_result == "DENY"))
    assert event is not None and len(event.entity_id) == 64
    assert value not in event.entity_id
    logs = emitted.getvalue()
    assert value not in logs and snapshot not in logs
    assert "/api/v1/people/{employee_id}" in logs


def test_database_failure_is_a_safe_503_and_recovers(client, login):
    login()
    original = app.dependency_overrides[get_db]

    def unavailable():
        raise OperationalError("private SQL sentinel", {}, RuntimeError("private error sentinel"))

    app.dependency_overrides[get_db] = unavailable
    try:
        response = client.get("/api/v1/me")
        assert response.status_code == 503
        assert response.json()["title"] == "Database Unavailable"
        assert "sentinel" not in response.text
        assert response.headers["cache-control"] == "no-store"
    finally:
        app.dependency_overrides[get_db] = original
    assert client.get("/api/v1/me").status_code == 200


def test_streamed_response_bound_never_publishes_a_partial_secret():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(3):
            await send(
                {"type": "http.response.body", "body": b"sensitive" * 100000, "more_body": True}
            )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=boundary.HttpBoundary(downstream)),
            base_url="http://127.0.0.1:3017",
        ) as client:
            response = await client.get("/")
            assert response.status_code == 503
            assert "sensitive" not in response.text
            assert response.json()["title"] == "Response Too Large"

    asyncio.run(exercise())


def test_stream_timeout_duplicate_lengths_and_disconnect(monkeypatch):
    called = []

    async def downstream(scope, receive, send):
        called.append(True)

    async def exercise(mode):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 3017),
            "client": ("127.0.0.1", 1),
            "headers": [(b"host", b"127.0.0.1:3017")],
        }
        if mode == "lengths":
            scope["headers"] += [(b"content-length", b"0"), (b"content-length", b"1")]
        messages = []

        async def receive():
            if mode == "disconnect":
                return {"type": "http.disconnect"}
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        await boundary.HttpBoundary(downstream)(scope, receive, send)
        if mode == "disconnect":
            assert messages == []
        else:
            assert messages[0]["status"] == (408 if mode == "timeout" else 400)
            assert len(json.loads(messages[1]["body"])["request_id"]) == 36

    monkeypatch.setattr(boundary, "BODY_TIMEOUT_SECONDS", 0.001)
    for mode in ("timeout", "lengths", "disconnect"):
        asyncio.run(exercise(mode))
    assert not called


def test_rate_limit_ignores_forged_forwarded_ip_and_expires():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limited = boundary.HttpBoundary(downstream)
    limited.limiter = boundary.RequestLimiter(read_limit=1, write_limit=1, max_keys=2)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=limited), base_url="http://127.0.0.1:3017"
        ) as client:
            assert (await client.get("/")).status_code == 204
            denied = await client.get("/", headers={"x-forwarded-for": "another-client"})
            assert denied.status_code == 429
            assert denied.headers["retry-after"] == "60"

    asyncio.run(exercise())
    limiter = boundary.RequestLimiter(1, 1, 1)
    assert limiter.admit("one", False, 0)
    assert not limiter.admit("one", False, 1)
    assert not limiter.admit("two", False, 1)
    assert limiter.admit("two", False, 61)
    assert len(limiter.windows) == 1
    with pytest.raises(ValueError):
        boundary.RequestLimiter(0, 1, 1)
    assert isinstance(next(iter(limiter.windows.values())), deque)
