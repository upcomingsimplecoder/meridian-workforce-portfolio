"""Reject SQL inputs and inconsistent financial subjects before any I/O."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from meridian.analytics_pipeline import sql_rows
from meridian.analytics_snowflake import SnowflakeConfig, load_relation


class NoConnection:
    def execute(self, *args, **kwargs):
        raise AssertionError("Unsafe projection reached SQL")

    def cursor(self):
        raise AssertionError("Unsafe staging path reached Snowflake")


@pytest.mark.security
@pytest.mark.parametrize(
    "table,columns",
    [
        ("dim_employee_scd2;DROP TABLE x", None),
        ("dim_employee_scd2", ['employee_id" FROM secrets--']),
        ("dim_employee_scd2", ["unknown"]),
        ("dim_employee_scd2", []),
        ("mart_workforce_kpis", ["headcount"]),
    ],
)
def test_analytics_projection_rejects_unknown_names_before_sql(table, columns):
    with pytest.raises(ValueError, match="Unknown curated"):
        sql_rows(NoConnection(), table, columns)


def snowflake_config():
    return SnowflakeConfig(
        "synthetic",
        "synthetic",
        Path("unused"),
        None,
        "MERIDIAN_ANALYTICS",
        "MERIDIAN_XS",
        "MERIDIAN_LOADER",
        "MERIDIAN_TRANSFORM",
    )


@pytest.mark.security
@pytest.mark.parametrize(
    "attack",
    ["quote_parent", "quoted_symlink_parent", "symlink_file", "unknown_relation", "invalid_run"],
)
def test_snowflake_staging_rejects_unsafe_resolved_locations_before_connection(tmp_path, attack):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / ("a" * 64 + ".parquet")
    path.write_bytes(b"synthetic")
    path.chmod(0o600)
    name, run = "workforce", str(uuid4())
    if attack == "quote_parent":
        target = tmp_path / "escape'"
        directory.rename(target)
        path = target / path.name
    elif attack == "quoted_symlink_parent":
        target = tmp_path / "escape'"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    elif attack == "symlink_file":
        target = directory / "real.parquet"
        path.rename(target)
        path.symlink_to(target)
    elif attack == "unknown_relation":
        name = "workforce;DROP TABLE x"
    elif attack == "invalid_run":
        run = "00000000-0000-0000-0000-000000000000';DROP TABLE x"
    with pytest.raises(ValueError):
        load_relation(NoConnection(), snowflake_config(), name, run, path, 1)


@pytest.mark.security
def test_optimized_python_keeps_financial_subject_checks():
    # Run the actual domain functions under -O, where Python assertions are removed.
    code = """
from meridian.replay_material import scenario_parts, award_parts
from meridian.replay_auth import Subject
from meridian.finance_engine import FinanceInputError
checks = [(scenario_parts, "PINNED_SCENARIO_SUBJECT_MISMATCH"), (award_parts, "PINNED_AWARD_SUBJECT_MISMATCH")]
for function, expected in checks:
    try:
        function(None, Subject("SCENARIO", object(), object(), None, False))
    except FinanceInputError as error:
        if str(error) != expected: raise SystemExit("Wrong explicit financial rejection")
    else: raise SystemExit("Invalid subject accepted under optimized Python")
print("2 explicit optimized financial rejections")
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2 explicit optimized financial rejections"
