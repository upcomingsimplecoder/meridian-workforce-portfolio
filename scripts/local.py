"""Project-owned local processes. Never touches a system PostgreSQL cluster."""

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values
from operator_db import admin_url

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
os.chdir(ROOT)


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def postgres_bin():
    configured = os.environ.get("MERIDIAN_PG_BIN")
    if configured:
        return Path(configured)
    homebrew = Path("/opt/homebrew/opt/postgresql@17/bin")
    if homebrew.exists():
        return homebrew
    executable = shutil.which("pg_ctl")
    if executable:
        return Path(executable).parent
    raise SystemExit(
        "PostgreSQL 17 binaries required. Install postgresql@17 (macOS) or PostgreSQL 17 (Linux); or use make compose-demo."
    )


def node_env():
    env = os.environ.copy()
    if Path("/opt/homebrew/opt/node@24/bin").exists():
        env["PATH"] = "/opt/homebrew/opt/node@24/bin:" + env["PATH"]
    return env


def bootstrap():
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    pgpass = LOCAL / "pg-password"
    if not pgpass.exists():
        pgpass.write_text(secrets.token_urlsafe(32))
        pgpass.chmod(0o600)
    password = pgpass.read_text().strip()
    admin_path = LOCAL / "admin-database-url"
    if not admin_path.exists():
        admin_path.write_text(
            f"postgresql+psycopg://meridian:{password}@127.0.0.1:54329/meridian_demo\n"
        )
        admin_path.chmod(0o600)
    if not (ROOT / ".env").exists():
        values = {
            "APP_ENV": "local",
            "DATABASE_URL": f"postgresql+psycopg://meridian:{password}@127.0.0.1:54329/meridian_demo",
            "OIDC_DATABASE_URL": f"postgresql://meridian:{password}@127.0.0.1:54329/meridian_demo",
            "OIDC_ISSUER": "http://127.0.0.1:4017",
            "OIDC_CLIENT_ID": "meridian-web",
            "OIDC_REDIRECT_URI": "http://127.0.0.1:3017/auth/callback",
            "WEB_ORIGIN": "http://127.0.0.1:3017",
            "API_ORIGIN": "http://127.0.0.1:8017",
            "COOKIE_SECURE": "false",
            "AI_ENABLED": "false",
            "LOCAL_DATA_DIR": str(LOCAL),
        }
        (ROOT / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
        (ROOT / ".env").chmod(0o600)
    binary = postgres_bin()
    cluster = LOCAL / "postgres"
    if not (cluster / "PG_VERSION").exists():
        run(
            [
                binary / "initdb",
                "-D",
                cluster,
                "-U",
                "meridian",
                "--pwfile",
                pgpass,
                "--auth-host=scram-sha-256",
                "--auth-local=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
    status = subprocess.run(
        [str(binary / "pg_ctl"), "-D", str(cluster), "status"], capture_output=True
    )
    if status.returncode:
        run(
            [
                binary / "pg_ctl",
                "-D",
                cluster,
                "-l",
                LOCAL / "postgres.log",
                "-o",
                f"-p 54329 -h 127.0.0.1 -k {LOCAL}",
                "-w",
                "start",
            ]
        )
    env = os.environ | {"PGPASSWORD": password}
    for database in ("meridian_demo", "meridian_test"):
        check = subprocess.run(
            [
                str(binary / "psql"),
                "-h",
                "127.0.0.1",
                "-p",
                "54329",
                "-U",
                "meridian",
                "-d",
                "postgres",
                "-tAc",
                f"SELECT 1 FROM pg_database WHERE datname='{database}'",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        if check.stdout.strip() != "1":
            run(
                [binary / "createdb", "-h", "127.0.0.1", "-p", "54329", "-U", "meridian", database],
                env=env,
            )
    run(["node", "scripts/identity-keys.mjs"], env=node_env())
    print("Native PostgreSQL ready on loopback port 54329; demo and test databases isolated.")


def reset():
    from meridian.config import settings
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    cfg = settings()
    operator_url = admin_url()
    url = make_url(operator_url)
    if (
        cfg.app_env not in {"local", "test"}
        or url.host not in {"localhost", "127.0.0.1", "postgres"}
        or url.database not in {"meridian_demo", "meridian_test"}
    ):
        raise SystemExit(
            "Refusing reset: only isolated local meridian_demo/meridian_test databases are allowed."
        )
    with create_engine(operator_url, hide_parameters=True).begin() as db:
        db.execute(text("DROP SCHEMA public CASCADE"))
        db.execute(text("CREATE SCHEMA public"))
    run([ROOT / ".venv/bin/python", "-m", "scripts.operator_db", "upgrade"])
    run([ROOT / ".venv/bin/python", "-m", "scripts.operator_db", "seed"])
    run([ROOT / ".venv/bin/python", "-m", "scripts.provision_db", "--test-db"])


def wait_for(url, label):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    raise SystemExit(f"{label} did not become ready. Inspect .local/{label}.log.")


def start():
    config = dotenv_values(ROOT / ".env")
    processes = {
        "api": (
            [
                str(ROOT / ".venv/bin/uvicorn"),
                "meridian.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8017",
                "--no-access-log",
            ],
            ROOT,
        ),
        "identity": (["node", "--env-file=.env", "apps/identity/server.mjs"], ROOT),
        "worker": ([str(ROOT / ".venv/bin/python"), "-m", "apps.worker.run"], ROOT),
        "analytics-worker": ([str(ROOT / ".venv/bin/python"), "-m", "apps.worker.analytics"], ROOT),
        "web": (["npm", "run", "start", "-w", "apps/web"], ROOT),
    }
    pids = {}
    for name, (cmd, cwd) in processes.items():
        pidfile = LOCAL / f"{name}.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 0)
                pids[name] = int(pidfile.read_text())
                continue
            except ProcessLookupError:
                pass
        with (LOCAL / f"{name}.log").open("a") as log:
            env = node_env()
            if name == "api":
                env["DATABASE_URL"] = config["DATABASE_URL"]
            elif name in {"worker", "analytics-worker"}:
                if not config.get("WORKER_DATABASE_URL"):
                    raise SystemExit("Run make setup to provision the standalone worker account.")
                env["WORKER_DATABASE_URL"] = config["WORKER_DATABASE_URL"]
                env["DATABASE_URL"] = config["WORKER_DATABASE_URL"]
            elif name == "identity":
                env["OIDC_DATABASE_URL"] = config["OIDC_DATABASE_URL"]
            process = subprocess.Popen(
                cmd, cwd=cwd, env=env, stdout=log, stderr=log, start_new_session=True
            )
        pidfile.write_text(str(process.pid))
        pids[name] = process.pid
    wait_for("http://127.0.0.1:8017/ready", "api")
    wait_for("http://127.0.0.1:4017/.well-known/openid-configuration", "identity")
    wait_for("http://127.0.0.1:3017", "web")
    print("Meridian running at http://127.0.0.1:3017. Credentials: .local/demo-credentials.json")
    print(json.dumps(pids))


def stop():
    for name in ("web", "analytics-worker", "worker", "identity", "api"):
        pidfile = LOCAL / f"{name}.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text())
                command = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
                ).stdout
                if any(
                    word in command
                    for word in (
                        "uvicorn",
                        "apps.worker.run",
                        "apps.worker.analytics",
                        "apps/identity/server.mjs",
                        "npm run start",
                    )
                ):
                    os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            pidfile.unlink()
    print("Stopped owned Meridian application processes; database retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["bootstrap", "reset", "up", "down"])
    action = parser.parse_args().action
    {"bootstrap": bootstrap, "reset": reset, "up": start, "down": stop}[action]()
