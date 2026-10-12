"""Round-trip only replay tables in the isolated test database, preserving prior data."""

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
new_tables = {"replay_capsule", "replay_attempt", "replay_artifact"}
prior = sorted(set(inspect(engine).get_table_names()) - new_tables - {"alembic_version"})


def counts():
    with engine.connect() as connection:
        return {
            table: connection.scalar(text('SELECT count(*) FROM "' + table + '"'))
            for table in prior
        }


with engine.connect() as connection:
    if any(connection.scalar(text("SELECT count(*) FROM " + table)) for table in new_tables):
        raise SystemExit(
            "Existing replay records must be preserved. A down/up rehearsal requires empty new test tables."
        )
before = counts()
env = os.environ | {"DATABASE_URL": url.render_as_string(hide_password=False), "APP_ENV": "test"}
results = []
for command in (
    [".venv/bin/alembic", "downgrade", "0009"],
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
        raise SystemExit("Migration rehearsal failed; inspect the captured command result")
after = counts()
if before != after:
    raise SystemExit("Replay migration changed prior table counts")
(
    root / f"docs/evidence/{os.environ.get('EVIDENCE_PHASE', 'p08')}-migration-roundtrip.json"
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
