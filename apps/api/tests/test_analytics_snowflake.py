"""Noncredentialed Snowflake configuration/SQL contracts; no cloud PASS claim."""

from pathlib import Path
from uuid import uuid4

import pytest
from meridian.analytics_exports import SCHEMAS
from meridian.analytics_snowflake import SnowflakeConfig, identifier, merge_sql, raw_ddl


@pytest.mark.parametrize(
    "unsafe", ["production; DROP TABLE x", "a.b", "lowercase", "A'", "A" * 102]
)
def test_dynamic_snowflake_identifiers_fail_closed(unsafe):
    with pytest.raises(ValueError):
        identifier(unsafe)


def test_snowflake_adapter_requires_operator_credentials_without_inventing_defaults(monkeypatch):
    for key in (
        "DBT_ENV_SECRET_MERIDIAN_SF_ACCOUNT",
        "DBT_ENV_SECRET_MERIDIAN_SF_USER",
        "DBT_ENV_SECRET_MERIDIAN_SF_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="UNVERIFIED"):
        SnowflakeConfig.from_env()


def test_raw_ddl_and_idempotent_merge_are_allowlisted_and_deduplicate_identical_rows():
    run_id = str(uuid4())
    for name in SCHEMAS:
        ddl = raw_ddl("MERIDIAN_ANALYTICS", name)
        sql = merge_sql("MERIDIAN_ANALYTICS", name, run_id)
        assert "RUN_ID VARCHAR(36) NOT NULL" in ddl and "ROW_SHA256 VARCHAR(64) NOT NULL" in ddl
        assert "SELECT DISTINCT" in sql and "target.RUN_ID = source.RUN_ID" in sql
        assert "target.ROW_SHA256 = source.ROW_SHA256" in sql and run_id in sql
        assert "WHEN MATCHED" not in sql
    with pytest.raises(ValueError):
        merge_sql("MERIDIAN_ANALYTICS", "payroll_secrets", run_id)
    with pytest.raises(ValueError):
        merge_sql("MERIDIAN_ANALYTICS", "comp", "not-a-uuid")


def test_real_snowflake_profile_has_separate_secret_names_roles_and_cost_controls():
    root = Path(__file__).resolve().parents[3]
    profile = (root / "analytics/snowflake/profiles.yml").read_text()
    setup = (root / "analytics/snowflake/bootstrap.sql").read_text()
    assert "DBT_ENV_SECRET_MERIDIAN_SF_KEY" in profile and "private_key_path" in profile
    assert "password:" not in profile and "STATEMENT_TIMEOUT_IN_SECONDS: 90" in profile
    assert "MERIDIAN_LOADER" in setup and "MERIDIAN_TRANSFORM" in setup and "MERIDIAN_READ" in setup
    assert "AUTO_SUSPEND = 60" in setup and "XSMALL" in setup
    assert "GRANT SELECT ON ALL TABLES" not in setup
