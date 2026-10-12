import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.operator_db import test_url as isolated_url  # noqa: E402

url = make_url(isolated_url())
if url.host not in {"localhost", "127.0.0.1", "postgres"}:
    raise RuntimeError("Integration tests require an isolated local database")
os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
os.environ["APP_ENV"] = "test"

from meridian.db import engine, get_db  # noqa: E402
from meridian.identity import digest  # noqa: E402
from meridian.main import app  # noqa: E402
from meridian.models import WebSession, now  # noqa: E402


@pytest.fixture
def db():
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as session:
            yield session
        transaction.rollback()


@pytest.fixture
def client(db, monkeypatch):
    from meridian import telemetry

    monkeypatch.setitem(
        telemetry._factories,
        "meridian-api",
        sessionmaker(
            bind=db.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
    )
    app.dependency_overrides[get_db] = lambda: db
    # Each test is an independent ASGI application lifetime. Keep real limits
    # active within the test, without inheriting another test's ingress window.
    app.middleware_stack = None
    with TestClient(app, base_url="http://127.0.0.1:3017") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def login(client, db):
    def authenticate(persona="hr"):
        raw = secrets.token_urlsafe(32)
        session = WebSession(
            token_hash=digest(raw),
            principal_id=persona,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now() + timedelta(hours=1),
        )
        db.add(session)
        db.commit()
        client.cookies.set("meridian_session", raw)
        client.headers.update(
            {"Origin": "http://127.0.0.1:3017", "X-CSRF-Token": session.csrf_token}
        )
        return session

    return authenticate
