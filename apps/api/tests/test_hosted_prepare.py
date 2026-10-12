import json
import stat

import pytest

from scripts.hosted_prepare import operator_url, private_json, same_project


@pytest.mark.security
@pytest.mark.parametrize(
    "value",
    [
        "",
        "private-malformed-url",
        "postgresql://postgres:test-only@localhost/postgres?sslmode=verify-full",
        "postgresql://postgres:test-only@db.fictional.supabase.co/postgres?sslmode=require",
        "postgresql://meridian_app:test-only@db.fictional.supabase.co/postgres?sslmode=verify-full",
        "postgresql://postgres:test-only@db.fictional.supabase.co/other-app?sslmode=verify-full",
    ],
)
def test_bootstrap_refuses_missing_operator_untrusted_host_or_runtime_account(value):
    with pytest.raises(RuntimeError) as error:
        operator_url({"MIGRATION_DATABASE_URL": value})
    assert "test-only" not in str(error.value)
    assert "private-malformed-url" not in str(error.value)


def test_bootstrap_accepts_direct_and_session_pooler_tls_urls():
    for value in (
        "postgresql://postgres:test-only@db.fictional.supabase.co/postgres?sslmode=verify-full",
        "postgresql+psycopg://postgres.fictional:test-only@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=verify-full",
    ):
        assert operator_url({"MIGRATION_DATABASE_URL": value}).database == "postgres"


@pytest.mark.security
def test_private_receipt_is_exclusive_and_mode_0600(tmp_path):
    path = tmp_path / "operator/runtime-urls.json"
    private_json(path, {"api": "private-test-only"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"api": "private-test-only"}
    with pytest.raises(FileExistsError):
        private_json(path, {"api": "changed"})
    assert json.loads(path.read_text()) == {"api": "private-test-only"}


@pytest.mark.security
@pytest.mark.parametrize(
    "storage",
    [
        "https://other-app.supabase.co",
        "http://fictional.supabase.co",
        "https://fictional.supabase.co/path",
        "https://fictional.supabase.co?token=private-test-only",
    ],
)
def test_cross_project_or_untrusted_storage_is_rejected_before_any_external_write(storage):
    target = operator_url(
        {
            "MIGRATION_DATABASE_URL": "postgresql://postgres:test-only@db.fictional.supabase.co/postgres?sslmode=verify-full"
        }
    )
    with pytest.raises(RuntimeError, match="same new Meridian project"):
        same_project({"SUPABASE_URL": storage}, target)


def test_direct_and_pooler_connections_match_the_same_storage_project():
    for uri in (
        "postgresql://postgres:test-only@db.fictional.supabase.co/postgres?sslmode=verify-full",
        "postgresql://postgres.fictional:test-only@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=verify-full",
    ):
        same_project(
            {"SUPABASE_URL": "https://fictional.supabase.co"},
            operator_url({"MIGRATION_DATABASE_URL": uri}),
        )
