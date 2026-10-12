"""Private configuration boundaries for standalone jobs and validation logs."""

from types import SimpleNamespace

import pytest
from meridian import db
from meridian.config import Settings
from pydantic import ValidationError


@pytest.mark.security
def test_invalid_hosted_settings_do_not_print_connection_credentials():
    secret = "operator-password-that-must-remain-private"
    with pytest.raises(ValidationError) as failure:
        Settings(
            _env_file=None,
            app_env="production",
            database_url=f"postgresql://user:{secret}@db/demo",
            cookie_secure=False,
        )
    assert secret not in str(failure.value)
    assert "Hosted operation requires HTTPS" in str(failure.value)


def test_standalone_worker_uses_separate_account(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        db,
        "settings",
        lambda: SimpleNamespace(worker_database_url="postgresql+psycopg://worker@localhost/jobs"),
    )
    factory = db.worker_session_factory()
    assert factory.kw["bind"].url.username == "worker"
    factory.kw["bind"].dispose()


def test_explicit_isolated_worker_connection_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://api@localhost/meridian_demo")
    monkeypatch.setenv(
        "WORKER_DATABASE_URL", "postgresql+psycopg://isolated@localhost/meridian_test"
    )
    factory = db.worker_session_factory()
    assert factory.kw["bind"].url.username == "isolated"
    assert factory.kw["bind"].url.database == "meridian_test"
    factory.kw["bind"].dispose()


@pytest.mark.security
def test_generic_api_connection_is_not_a_worker_fallback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://api@localhost/meridian_demo")
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "settings", lambda: SimpleNamespace(worker_database_url=""))
    with pytest.raises(RuntimeError, match="WORKER_DATABASE_URL is required"):
        db.worker_session_factory()


@pytest.mark.security
def test_missing_worker_account_fails_before_connecting(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "settings", lambda: SimpleNamespace(worker_database_url=""))
    with pytest.raises(RuntimeError, match="WORKER_DATABASE_URL is required"):
        db.worker_session_factory()


def hosted_values():
    return {
        "app_env": "production",
        "runtime_kind": "api",
        "database_url": "postgresql+psycopg://meridian_app:test-only@db.fictional.supabase.co/postgres?sslmode=verify-full",
        "oidc_issuer": "https://identity.example",
        "web_origin": "https://meridian.example",
        "api_origin": "https://api.example",
        "oidc_redirect_uri": "https://meridian.example/auth/callback",
        "cookie_secure": True,
        "storage_backend": "supabase",
        "supabase_url": "https://fictional.supabase.co",
        "supabase_service_role_key": "storage-test-only",
        "object_signing_key": "a1" * 32,
    }


def test_hosted_settings_accept_exact_origins_and_dedicated_tls_role():
    values = hosted_values()
    assert Settings(_env_file=None, **values).runtime_kind == "api"
    values.update(
        runtime_kind="worker",
        database_url=values["database_url"].replace("meridian_app", "meridian_worker"),
    )
    assert Settings(_env_file=None, **values).runtime_kind == "worker"


def test_analytics_timeout_covers_measured_cold_run_and_has_a_hard_upper_bound():
    assert Settings(_env_file=None, **hosted_values()).analytics_dbt_timeout_seconds == 600
    for value in (299, 901):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                **hosted_values(),
                analytics_dbt_timeout_seconds=value,
            )


@pytest.mark.security
@pytest.mark.parametrize(
    "field,value",
    [
        ("api_origin", "http://api.example"),
        ("oidc_internal_origin", "http://identity.example"),
        ("oidc_internal_origin", "https://other-issuer.example"),
        ("web_origin", "https://meridian.example/path"),
        ("oidc_issuer", "https://identity.example/"),
        ("oidc_redirect_uri", "https://other.example/auth/callback"),
        ("oidc_redirect_uri", "https://meridian.example/auth/callback?next=/"),
        (
            "database_url",
            "postgresql+psycopg://postgres:private-operator@db.example/postgres?sslmode=verify-full",
        ),
        (
            "database_url",
            "postgresql+psycopg://meridian_app:private-operator@db.example/postgres?sslmode=require",
        ),
        ("storage_backend", "local"),
        ("supabase_url", "http://fictional.supabase.co"),
        ("supabase_url", "https://other-app.supabase.co"),
        ("supabase_url", "https://untrusted.example"),
        ("object_signing_key", ""),
    ],
)
def test_hosted_settings_fail_closed_without_echoing_credentials(field, value):
    values = hosted_values() | {field: value}
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert "private-operator" not in str(error.value)
    assert "storage-test-only" not in str(error.value)


@pytest.mark.security
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://meridian_app:test-only@db.example/postgres?sslmode=verify-full",
        "postgresql+psycopg://meridian_worker:test-only@db.example/postgres?sslmode=require",
        "postgresql+psycopg://postgres:private-operator@db.example/postgres?sslmode=verify-full",
    ],
)
def test_hosted_worker_rejects_api_operator_and_unverified_tls(monkeypatch, url):
    monkeypatch.setenv("WORKER_DATABASE_URL", url)
    monkeypatch.setattr(
        db, "settings", lambda: SimpleNamespace(app_env="production", worker_database_url=url)
    )
    with pytest.raises(RuntimeError, match="dedicated TLS database credential") as error:
        db.worker_session_factory()
    assert "private-operator" not in str(error.value)
