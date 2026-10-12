"""Operator database commands; the runtime API never imports or reads this module."""

import argparse
import os
import subprocess
from pathlib import Path

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def admin_url() -> str:
    configured = os.environ.get("MIGRATION_DATABASE_URL")
    path = ROOT / ".local/admin-database-url"
    if configured:
        return configured
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(
            "Private operator database configuration is required. Run make bootstrap."
        )
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Operator database file must have mode 0600")
    return path.read_text().strip()


def test_url() -> str:
    url = make_url(os.environ.get("TEST_DATABASE_URL") or admin_url()).set(database="meridian_test")
    if url.host not in {"127.0.0.1", "localhost", "postgres"}:
        raise RuntimeError("Only the isolated local meridian_test database is permitted")
    return url.render_as_string(hide_password=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["upgrade", "downgrade", "check", "seed"])
    parser.add_argument("revision", nargs="?")
    args = parser.parse_args()
    if args.operation == "seed":
        command = [str(ROOT / ".venv/bin/python"), "-m", "meridian.seed"]
    else:
        command = [str(ROOT / ".venv/bin/alembic"), args.operation]
        if args.operation in {"upgrade", "downgrade"}:
            command.append(args.revision or ("head" if args.operation == "upgrade" else "base"))
    result = subprocess.run(
        command, cwd=ROOT, env=os.environ | {"DATABASE_URL": admin_url()}, check=False
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
