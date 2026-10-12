"""Round-trip the new migration on the isolated test DB; never reset the demo."""

import json
import os
import subprocess
from pathlib import Path

from operator_db import test_url
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
url = make_url(test_url())
if url.host not in {"127.0.0.1", "localhost"}:
    raise SystemExit("Migration verification requires the isolated native test database")
engine = create_engine(url, hide_parameters=True)
new_tables = {
    "analytics_run",
    "analytics_artifact",
    "analytics_delivery",
    "analytics_result",
    "analytics_publication",
}
prior = sorted(set(inspect(engine).get_table_names()) - new_tables - {"alembic_version"})


def counts():
    with engine.connect() as connection:
        return {
            table: connection.scalar(text('SELECT count(*) FROM "' + table + '"'))
            for table in prior
        }


with engine.connect() as connection:
    if connection.scalar(text("SELECT count(*) FROM analytics_run")):
        raise SystemExit(
            "Preserve existing analytical test runs; this round trip requires no analytical runs"
        )
before = counts()
env = os.environ | {"DATABASE_URL": url.render_as_string(hide_password=False), "APP_ENV": "test"}
results = []
for command in (
    [".venv/bin/alembic", "downgrade", "0008"],
    [".venv/bin/alembic", "upgrade", "head"],
    [".venv/bin/alembic", "check"],
    [".venv/bin/python", "-m", "meridian.seed"],
):
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    results.append(
        {
            "command": command,
            "exit_code": result.returncode,
            "output": result.stdout + result.stderr,
        }
    )
    if result.returncode:
        raise SystemExit("Migration verification failed; inspect the captured command result")
after = counts()
if before != after:
    raise SystemExit("The migration changed prior table counts")
(
    root / f"docs/evidence/{os.environ.get('EVIDENCE_PHASE', 'p07')}-migration-roundtrip.json"
).write_text(
    json.dumps(
        {
            "database": "isolated meridian_test",
            "preserved_tables": len(prior),
            "before": before,
            "after": after,
            "commands": results,
        },
        indent=2,
    )
    + "\n"
)
print(f"PASS migration down/up/check/reseed; {len(prior)} prior table counts unchanged")
