"""Optional operator adapter. Local contract tests do not validate a Snowflake account."""

import importlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from meridian.analytics_exports import SCHEMAS


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", value):
        raise ValueError(
            "Snowflake identifiers require bounded uppercase letters, numbers and underscores"
        )
    return value


@dataclass(frozen=True, repr=False)
class SnowflakeConfig:
    account: str
    user: str
    key_file: Path
    key_password: str | None
    database: str
    warehouse: str
    loader_role: str
    transform_role: str

    @classmethod
    def from_env(cls) -> "SnowflakeConfig":
        required = (
            "DBT_ENV_SECRET_MERIDIAN_SF_ACCOUNT",
            "DBT_ENV_SECRET_MERIDIAN_SF_USER",
            "DBT_ENV_SECRET_MERIDIAN_SF_KEY",
            "MERIDIAN_SF_DATABASE",
            "MERIDIAN_SF_WAREHOUSE",
            "MERIDIAN_SF_LOADER_ROLE",
            "MERIDIAN_SF_TRANSFORM_ROLE",
        )
        if any(not os.environ.get(name) for name in required):
            raise ValueError(
                "Snowflake validation UNVERIFIED: operator account, key and scoped roles are required"
            )
        key = Path(os.environ[required[2]]).expanduser().resolve()
        if not key.is_file() or key.stat().st_mode & 0o077:
            raise ValueError("Snowflake private key must exist and have mode 0600 or stricter")
        return cls(
            os.environ[required[0]],
            os.environ[required[1]],
            key,
            os.environ.get("DBT_ENV_SECRET_MERIDIAN_SF_PASSPHRASE"),
            *(identifier(os.environ[name]) for name in required[3:]),
        )

    def connect(self, *, transform: bool = False) -> Any:
        connector = importlib.import_module("snowflake.connector")
        return connector.connect(
            account=self.account,
            user=self.user,
            private_key_file=str(self.key_file),
            private_key_file_pwd=self.key_password,
            database=self.database,
            schema="MERIDIAN_RAW",
            warehouse=self.warehouse,
            role=self.transform_role if transform else self.loader_role,
            autocommit=False,
            paramstyle="qmark",
            login_timeout=20,
            network_timeout=60,
            session_parameters={
                "TIMEZONE": "UTC",
                "STATEMENT_TIMEOUT_IN_SECONDS": 90,
                "ERROR_ON_NONDETERMINISTIC_MERGE": True,
                "QUERY_TAG": "meridian_synthetic_validation",
            },
        )


def raw_ddl(database: str, name: str) -> str:
    database = identifier(database)
    if name not in SCHEMAS:
        raise ValueError("Unknown allowlisted analytical relation")
    columns = ["RUN_ID VARCHAR(36) NOT NULL"]
    columns.extend(
        key.upper() + " " + ("TIMESTAMP_TZ" if value == "TIMESTAMPTZ" else value)
        for key, value in SCHEMAS[name].items()
    )
    columns.append("ROW_SHA256 VARCHAR(64) NOT NULL")
    return (
        f"CREATE TABLE IF NOT EXISTS {database}.MERIDIAN_RAW.{name.upper()} ("
        + ", ".join(columns)
        + ")"
    )


def merge_sql(database: str, name: str, run_id: str) -> str:
    database = identifier(database)
    run_id = str(UUID(run_id))
    if name not in SCHEMAS:
        raise ValueError("Unknown allowlisted analytical relation")
    columns = ["RUN_ID", *(key.upper() for key in SCHEMAS[name]), "ROW_SHA256"]
    projection = ", ".join([f"'{run_id}' AS RUN_ID", *columns[1:]])
    return (
        f"MERGE INTO {database}.MERIDIAN_RAW.{name.upper()} target USING (SELECT DISTINCT {projection} FROM MERIDIAN_LOAD_{name.upper()}) source ON target.RUN_ID = source.RUN_ID AND target.ROW_SHA256 = source.ROW_SHA256 WHEN NOT MATCHED THEN INSERT ("  # noqa: S608 - bounded identifier, relation allowlist and canonical UUID; row values bound.
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join("source." + column for column in columns)
        + ")"
    )


def load_relation(
    connection: Any,
    config: SnowflakeConfig,
    name: str,
    run_id: str,
    path: Path,
    expected_count: int,
) -> int:
    run_id = str(UUID(run_id))
    resolved = path.resolve()
    if (
        name not in SCHEMAS
        or not re.fullmatch(r"[0-9a-f]{64}\.parquet", path.name)
        or path.is_symlink()
        or not path.is_file()
        or "'" in str(resolved)
    ):
        raise ValueError("Only a verified private Parquet artifact can be staged")
    database = identifier(config.database)
    with connection.cursor() as cursor:
        cursor.execute(raw_ddl(database, name))
        cursor.execute(
            f"CREATE OR REPLACE TEMPORARY TABLE MERIDIAN_LOAD_{name.upper()} LIKE {database}.MERIDIAN_RAW.{name.upper()}"
        )
        cursor.execute(f"ALTER TABLE MERIDIAN_LOAD_{name.upper()} DROP COLUMN RUN_ID")
        location = f"@{database}.MERIDIAN_RAW.MERIDIAN_EXPORTS/{run_id}/{name}"
        cursor.execute(
            f"PUT 'file://{path.resolve()}' {location} AUTO_COMPRESS=FALSE OVERWRITE=FALSE"
        )
        cursor.execute(
            f"COPY INTO MERIDIAN_LOAD_{name.upper()} FROM {location} FILES=('{path.name}') FILE_FORMAT=(TYPE=PARQUET) MATCH_BY_COLUMN_NAME=CASE_INSENSITIVE ON_ERROR=ABORT_STATEMENT FORCE=TRUE"
        )
        cursor.execute(f"SELECT count(*) FROM MERIDIAN_LOAD_{name.upper()}")  # noqa: S608 - bounded identifier, relation allowlist and canonical UUID; row values bound.
        if cursor.fetchone()[0] != expected_count:
            raise ValueError("Snowflake COPY row count differs from the frozen export")
        cursor.execute(merge_sql(config.database, name, run_id))
        cursor.execute(
            f"SELECT count(*) FROM {database}.MERIDIAN_RAW.{name.upper()} WHERE RUN_ID = ?",  # noqa: S608 - bounded identifier, relation allowlist and canonical UUID; row values bound.
            (run_id,),
        )
        observed = cursor.fetchone()[0]
        if observed != expected_count:
            raise ValueError("Snowflake MERGE row count differs from the frozen export")
    connection.commit()
    return int(observed)


class SnowflakeReadAdapter:
    """Static curated SQL casing adapter for the shared count/sum/hash comparator."""

    def __init__(self, connection: Any):
        self.connection = connection

    def execute(self, sql: str) -> "SnowflakeReadAdapter":
        # sql_rows accepts only internal, allowlisted relations and schema columns.
        adapted = re.sub(r'"([a-z_][a-z0-9_]*)"', lambda match: match.group(0).upper(), sql)
        self.cursor = self.connection.cursor().execute(adapted)
        self.description = [(item[0].lower(), *item[1:]) for item in self.cursor.description]
        return self

    def fetchall(self) -> Any:
        try:
            return self.cursor.fetchall()
        finally:
            self.cursor.close()
