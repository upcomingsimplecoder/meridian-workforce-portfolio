"""Operator-only bootstrap for a new, empty Meridian Supabase project.

Reads managed secrets from the environment. Does not deploy or print credentials.
Runtime services must never execute this task or receive MIGRATION_DATABASE_URL.
"""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
from sqlalchemy.engine import make_url

from scripts.provision_db import KNOWN_TABLES, ROLE_NAMES, provision

ROOT = Path(__file__).resolve().parents[1]


def operator_url(env):
    value = env.get("MIGRATION_DATABASE_URL", "")
    try:
        url = make_url(value)
    except Exception:
        raise RuntimeError("An explicit Supabase operator URL is required") from None
    if (
        url.drivername not in {"postgresql", "postgresql+psycopg"}
        or url.query.get("sslmode") != "verify-full"
        or not (url.host or "").endswith((".supabase.co", ".pooler.supabase.com"))
        or (url.username or "").split(".")[0] != "postgres"
        or url.database != "postgres"
        or not url.password
    ):
        raise RuntimeError("Bootstrap requires a dedicated new Supabase project and verified TLS")
    return url


def same_project(env, target):
    # A Storage key must never direct this operator task to another app.
    if (target.host or "").startswith("db."):
        project = target.host.split(".")[1]
    elif "." in (target.username or ""):
        project = target.username.split(".", 1)[1]
    else:
        raise RuntimeError("The operator connection must identify its Supabase project")
    storage = urlsplit(env.get("SUPABASE_URL", ""))
    if (
        storage.scheme != "https"
        or storage.hostname != project + ".supabase.co"
        or storage.username
        or storage.password
        or storage.path not in {"", "/"}
        or storage.query
        or storage.fragment
    ):
        raise RuntimeError("Storage and database must belong to the same new Meridian project")


def operator_environment():
    target = operator_url(os.environ)
    same_project(os.environ, target)
    # This mode exists only within an explicit operator task. The guarded URL
    # is never passed to a web server, worker, or identity process.
    os.environ["APP_ENV"] = "test"
    os.environ["DATABASE_URL"] = target.set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )
    os.environ["STORAGE_BACKEND"] = "supabase"
    from meridian.config import settings

    settings.cache_clear()
    return target


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def create_bucket():
    target = operator_environment()
    with psycopg.connect(
        target.set(drivername="postgresql").render_as_string(hide_password=False)
    ) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        if (
            tables != KNOWN_TABLES
            or db.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] != "0011"
        ):
            raise RuntimeError("Prepare the dedicated Meridian schema before creating storage")
        if db.execute("SELECT count(*) FROM pg_policies WHERE schemaname='storage'").fetchone()[0]:
            raise RuntimeError("Meridian storage must not have browser access policies")
    from meridian.config import Settings
    from meridian.object_store import (
        MAX_METADATA_BYTES,
        MAX_OBJECT_BYTES,
        _bounded_response,
        _require_private_bucket,
        _supabase_base,
        _supabase_headers,
    )

    cfg = Settings(_env_file=None)
    if cfg.storage_backend != "supabase":
        raise RuntimeError("Private Supabase storage configuration is required")
    with httpx.Client(timeout=cfg.storage_timeout_seconds) as client:
        with client.stream(
            "POST",
            _supabase_base() + "/bucket",
            headers=_supabase_headers(),
            json={
                "id": cfg.supabase_storage_bucket,
                "name": cfg.supabase_storage_bucket,
                "public": False,
                "file_size_limit": MAX_OBJECT_BYTES,
                "allowed_mime_types": ["application/octet-stream"],
            },
        ) as response:
            _bounded_response(response, MAX_METADATA_BYTES)
            if response.status_code not in {200, 201, 400, 409}:
                raise RuntimeError("Private bucket provisioning failed")
        # A duplicate bucket response never authorizes changing an existing
        # bucket or publishing it. Inspect its actual configuration first.
        _require_private_bucket(client)
    print("Private artifact bucket configuration verified.")


def bootstrap():
    target = operator_environment()
    from meridian.object_store import ensure_key

    # Validate managed signing material before touching a fresh database.
    ensure_key()
    directory = ROOT / ".local/hosted-bootstrap"
    credentials = directory / "runtime-database-credentials.json"
    if credentials.exists() or (directory / "demo-credentials.json").exists():
        raise RuntimeError("Bootstrap receipts already exist; review rather than rerun seeding")
    uri = target.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(uri) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        if tables:
            raise RuntimeError("Bootstrap requires a new project with an empty public schema")
        if db.execute("SELECT count(*) FROM pg_policies WHERE schemaname='storage'").fetchone()[0]:
            raise RuntimeError("A new private project must not have existing Storage policies")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    env = os.environ | {
        "APP_ENV": "test",  # Operator-only existing synthetic seed entry point.
        "DATABASE_URL": target.set(drivername="postgresql+psycopg").render_as_string(
            hide_password=False
        ),
        "LOCAL_DATA_DIR": str(directory),
        "AI_ENABLED": "false",
    }
    # Run the existing migrations and seed as operator, never at runtime startup.
    for command in (
        [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
        [str(ROOT / ".venv/bin/python"), "-m", "meridian.seed"],
    ):
        subprocess.run(command, cwd=ROOT, env=env, check=True)
    passwords = {role: secrets.token_urlsafe(32) for role in ROLE_NAMES.values()}
    private_json(credentials, passwords)
    provision(uri, passwords, supabase=True)
    runtime = {}
    for name, role in ROLE_NAMES.items():
        url = target.set(
            username=role
            + ("." + target.username.split(".", 1)[1] if "." in target.username else ""),
            password=passwords[role],
        )
        driver = "postgresql" if name == "identity" else "postgresql+psycopg"
        runtime[name] = url.set(drivername=driver).render_as_string(hide_password=False)
        with psycopg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False)
        ) as db:
            if db.execute("SELECT current_user").fetchone()[0] != role:
                raise RuntimeError("Runtime login did not resolve to its dedicated role")
            table = "principal" if name == "identity" else "alembic_version"
            db.execute(f"SELECT count(*) FROM public.{table}").fetchone()
    private_json(directory / "runtime-urls.json", runtime)
    with psycopg.connect(uri) as db:
        if db.execute(
            "SELECT count(*) FROM principal WHERE oidc_sub NOT LIKE 'synthetic:%'"
        ).fetchone()[0]:
            raise RuntimeError("Non-synthetic identity found")
        if db.execute("SELECT count(*) FROM principal").fetchone()[0] != 8:
            raise RuntimeError("Synthetic identity inventory mismatch")
        tables = {
            row[0]
            for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        if tables != KNOWN_TABLES:
            raise RuntimeError("Bootstrap schema inventory mismatch")
    print("New Meridian schema, eight fictional accounts and separate runtime grants verified.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["bootstrap", "storage"])
    args = parser.parse_args()
    if args.operation == "bootstrap":
        bootstrap()
    else:
        create_bucket()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Driver exceptions can include URLs. Keep the failure message safe;
        # operator checks below are rerunnable independently without reseeding.
        print("Hosted preparation failed; no runtime deployment was performed.")
        raise SystemExit(1) from None
