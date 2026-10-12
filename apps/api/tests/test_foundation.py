import json
from datetime import timedelta

import jwt
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.hazmat.primitives.asymmetric import rsa
from meridian.config import Settings
from meridian.db import engine
from meridian.governance import record
from meridian.identity import validate_id_token
from meridian.main import SUPPORTED_SCHEMA_REVISION
from meridian.models import AuditEvent, ConsumerReceipt, OutboxEvent, Principal, now, uid
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from apps.worker import run as worker


def test_real_postgres_and_health(client, db):
    assert db.bind.dialect.name == "postgresql"
    assert (
        db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        == SUPPORTED_SCHEMA_REVISION
    )
    assert client.get("/health").json() == {"status": "ok", "service": "meridian-api"}
    assert client.get("/ready").status_code == 200


@pytest.mark.parametrize("markers", [[], ["0010"], ["9999"], ["0010", "0011"]])
@pytest.mark.security
def test_readiness_refuses_empty_stale_future_or_multiple_migration_markers(client, markers):
    # The production probe uses a real connection; change only the dedicated
    # meridian_test marker and always restore it, including on a failed assertion.
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "meridian_test"
        original = list(connection.scalars(text("SELECT version_num FROM alembic_version")))
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
            for marker in markers:
                connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:marker)"),
                    {"marker": marker},
                )
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["type"] == "urn:meridian:problem:schema_unavailable"
        assert "postgresql" not in response.text
        assert "version_num" not in response.text
        assert client.get("/health").status_code == 200
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
            for marker in original:
                connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:marker)"),
                    {"marker": marker},
                )
    assert client.get("/ready").status_code == 200


@pytest.mark.parametrize("route", ["/api/v1/me", "/api/v1/overview"])
def test_unauthenticated_denied(client, route):
    response = client.get(route)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "persona",
    ["hr", "finance", "manager-a", "manager-b", "approver", "executive", "auditor", "steward"],
)
def test_db_grants_and_live_overview(client, login, persona):
    login(persona)
    me = client.get("/api/v1/me?role=hr&scope=*").json()
    assert me["id"] == persona
    assert (
        all(grant["role"] != "hr" for grant in me["grants"])
        if persona != "hr"
        else me["grants"][0]["role"] == "hr"
    )
    data = client.get("/api/v1/overview").json()
    assert data["demo_accounts"] == 8
    assert data["source"] == "PostgreSQL / live query"
    assert data["ai_enabled"] is False
    assert "password" not in json.dumps(me)


@pytest.mark.parametrize("mode", ["expired", "revoked", "deactivated", "forged"])
def test_invalid_sessions_fail_closed(client, login, db, mode):
    session = login("manager-a")
    if mode == "expired":
        session.expires_at = now() - timedelta(seconds=1)
    elif mode == "revoked":
        session.revoked = True
    elif mode == "deactivated":
        db.get(Principal, "manager-a").active = False
    else:
        client.cookies.set("meridian_session", "forged-hr-token")
    db.commit()
    assert client.get("/api/v1/me").status_code == 401


@pytest.mark.parametrize("mode", ["missing_csrf", "wrong_csrf", "foreign_origin", "missing_origin"])
def test_csrf_rejected_and_audited(client, login, db, mode):
    login()
    if mode == "missing_csrf":
        del client.headers["x-csrf-token"]
    elif mode == "wrong_csrf":
        client.headers["x-csrf-token"] = "wrong"
    elif mode == "foreign_origin":
        client.headers["origin"] = "https://untrusted.example"
    else:
        del client.headers["origin"]
    assert client.post("/api/v1/logout").status_code == 403
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.verb == "security.csrf_denied")
        )
        == 1
    )
    assert client.get("/api/v1/me").status_code == 200


def test_logout_is_durable(client, login, db):
    session = login()
    assert client.post("/api/v1/logout").status_code == 204
    db.refresh(session)
    assert session.revoked
    assert client.get("/api/v1/me").status_code == 401
    assert (
        db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "identity.logout.v1")
        )
        == 1
    )


def test_login_requires_browser_bound_state(client):
    response = client.get("/auth/login", follow_redirects=False)
    assert "code_challenge_method=S256" in response.headers["location"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/auth/callback?code=fake&state=fake").status_code == 400


@pytest.mark.parametrize(
    "alter", ["none", "expiry", "issuer", "audience", "nonce", "signature", "algorithm"]
)
def test_oidc_verification(alter):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {
        "kid": "test",
        "alg": "RS256",
    }
    claims = {
        "sub": "synthetic:hr",
        "iss": "http://127.0.0.1:4017",
        "aud": "meridian-web",
        "iat": now(),
        "exp": now() + timedelta(minutes=5),
        "nonce": "bound-nonce",
    }
    if alter == "expiry":
        claims["exp"] = now() - timedelta(minutes=1)
    if alter == "issuer":
        claims["iss"] = "https://attacker.example"
    if alter == "audience":
        claims["aud"] = "other-client"
    if alter == "nonce":
        claims["nonce"] = "wrong"
    if alter == "signature":
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        claims,
        "deliberately-wrong-algorithm-test-key-32bytes" if alter == "algorithm" else private,
        algorithm="HS256" if alter == "algorithm" else "RS256",
        headers={"kid": "test"},
    )
    if alter == "none":
        assert validate_id_token(token, {"keys": [public]}, "bound-nonce")["sub"] == "synthetic:hr"
    else:
        with pytest.raises(jwt.PyJWTError):
            validate_id_token(token, {"keys": [public]}, "bound-nonce")


def test_audit_and_outbox_atomic_rollback(db):
    before = db.scalar(select(func.count()).select_from(AuditEvent))
    nested = db.begin_nested()
    record(db, "hr", "identity.login", "principal", "hr", uid())
    db.flush()
    nested.rollback()
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == before
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_audit_append_only_at_database(db):
    audit = record(db, "hr", "identity.login", "principal", "hr", uid())
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("UPDATE audit_event SET verb='changed' WHERE id=:id"), {"id": audit.id})


def test_worker_duplicate_delivery_and_poison_retry(db, monkeypatch):
    record(db, "hr", "identity.login", "principal", "hr", uid())
    db.commit()
    monkeypatch.setattr(
        worker,
        "SessionLocal",
        sessionmaker(
            bind=db.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
    )
    assert worker.tick() == 1
    event = db.scalar(select(OutboxEvent))
    db.refresh(event)
    assert event.state == "DONE"
    event.state = "PENDING"
    db.commit()
    assert worker.tick() == 1
    assert db.scalar(select(func.count()).select_from(ConsumerReceipt)) == 1
    poison = OutboxEvent(
        event_type="unrecognized.v99",
        schema_version=99,
        aggregate_id="test",
        payload={},
        request_id=uid(),
    )
    db.add(poison)
    db.commit()
    for attempt in range(5):
        assert worker.tick() == 0
        db.refresh(poison)
        assert poison.attempts == attempt + 1
        poison.next_attempt_at = now() - timedelta(seconds=1)
        db.commit()
    assert poison.state == "DEAD"


def test_hosted_configuration_rejects_insecure_identity():
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(app_env="production", database_url="postgresql://unused", cookie_secure=False)


def test_request_size_and_validation_redaction(client):
    response = client.post("/api/v1/logout", headers={"content-length": str(9 * 1024 * 1024)})
    assert response.status_code == 413
    assert "salary" not in response.text
