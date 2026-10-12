"""Explicit credentialed operator gate. Never invoked as a simulated cloud test."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

from meridian.analytics_models import AnalyticsRun
from meridian.analytics_pipeline import compare_report, verified_result
from meridian.analytics_snowflake import SnowflakeConfig, SnowflakeReadAdapter, load_relation
from meridian.config import settings
from meridian.db import SessionLocal
from meridian.object_store import read_private
from meridian.policy import canonical_hash

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(
    description="Load a reconciled synthetic run and verify real Snowflake parity"
)
parser.add_argument("run_id", type=UUID)
parser.add_argument("--dbt", type=Path, default=root / ".local/snowflake-venv/bin/dbt")
args = parser.parse_args()
try:
    config = SnowflakeConfig.from_env()
    if not args.dbt.is_file():
        raise ValueError("Snowflake operator venv is missing; follow docs/SNOWFLAKE.md")
    with SessionLocal() as db:
        run = db.get(AnalyticsRun, str(args.run_id))
        if run is None:
            raise ValueError("Select an existing local analytical run")
        result, manifest = verified_result(db, run)
        if result.status != "PASS":
            raise ValueError("Snowflake validation requires a locally reconciled run")
        directory = Path(settings().local_data_dir).resolve() / "snowflake-validation" / run.id
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        schema = "MERIDIAN_MARTS_" + args.run_id.hex.upper()
        with config.connect() as connection:
            counts = {}
            for artifact in manifest["manifest"]["artifacts"]:
                path = directory / (artifact["sha256"] + ".parquet")
                path.write_bytes(read_private(artifact["sha256"]))
                path.chmod(0o600)
                counts[artifact["name"]] = load_relation(
                    connection, config, artifact["name"], run.id, path, artifact["row_count"]
                )
        command = [
            str(args.dbt.resolve()),
            "--log-path",
            str(directory / "logs"),
            "build",
            "--project-dir",
            str(root / "analytics"),
            "--profiles-dir",
            str(root / "analytics/snowflake"),
            "--target-path",
            str(directory / "dbt-target"),
            "--select",
            "tag:governed",
            "--vars",
            json.dumps({"business_run": True, "run_id": run.id}),
        ]
        outcome = subprocess.run(
            command,
            env=os.environ
            | {"MERIDIAN_SF_RUN_SCHEMA": schema, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"},
            capture_output=True,
            timeout=180,
        )
        log = directory / "dbt-build.log"
        log.write_bytes(outcome.stdout + outcome.stderr)
        log.chmod(0o600)
        if outcome.returncode:
            raise ValueError(
                "Credentialed Snowflake dbt build failed; inspect the private operator log"
            )
        tests = json.loads((directory / "dbt-target/run_results.json").read_text())["results"]
        if not tests or any(test["status"] not in {"success", "pass"} for test in tests):
            raise ValueError("Credentialed Snowflake dbt models/tests did not all pass")
        with config.connect(transform=True) as connection:
            connection.cursor().execute(f"USE SCHEMA {config.database}.{schema}").close()
            observed = compare_report(SnowflakeReadAdapter(connection), run)
        expected = {key: value for key, value in result.report.items() if key != "loaded_artifacts"}
        if canonical_hash(observed) != canonical_hash(expected) or not all(
            row["matches"] for row in observed["comparisons"]
        ):
            raise ValueError("Credentialed Snowflake count/sum/semantic-hash parity failed")
        evidence = {
            "validation": "CREDENTIALED PASS",
            "run_id": run.id,
            "model_sha256": run.model_sha256,
            "inputs_sha256": run.inputs_sha256,
            "source_counts": counts,
            "dbt_nodes": len(tests),
            "comparison_count": len(observed["comparisons"]),
            "report_sha256": canonical_hash(observed),
        }
        output = directory / "verified-summary.json"
        output.write_text(json.dumps(evidence, indent=2) + "\n")
        output.chmod(0o600)
        print(f"CREDENTIALED PASS; private evidence: {output}")
except Exception as error:
    # Connector failures can include account names or SQL. Never print exception text.
    print(
        f"Snowflake validation FAILED / UNVERIFIED ({type(error).__name__}); no cloud result published. See docs/SNOWFLAKE.md and private operator logs."
    )
    raise SystemExit(1) from None
