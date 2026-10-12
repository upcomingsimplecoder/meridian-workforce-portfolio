"""Prepare the dedicated integration database, never the demo database."""

import os
import subprocess

from operator_db import test_url as isolated_url
from sqlalchemy.engine import make_url

test_url = make_url(isolated_url())
if test_url.database != "meridian_test" or test_url.host not in {
    "127.0.0.1",
    "localhost",
    "postgres",
}:
    raise SystemExit("Test database must be isolated local meridian_test")
env = os.environ | {
    "DATABASE_URL": test_url.render_as_string(hide_password=False),
    "APP_ENV": "test",
}
subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=env, check=True)
subprocess.run([".venv/bin/python", "-m", "meridian.seed"], env=env, check=True)
print("Dedicated meridian_test database migrated and seeded.")
