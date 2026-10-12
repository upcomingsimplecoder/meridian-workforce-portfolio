"""Free demo supervisor. Each child uses its own database account.

Render restarts the service if any required child exits. PostgreSQL owns pending
work and Supabase owns artifacts; container files are disposable. The regular
worker entry points remain available for separate paid worker services.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
COMMON = {
    "PATH",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "APP_ENV",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_REDIRECT_URI",
    "WEB_ORIGIN",
    "API_ORIGIN",
    "COOKIE_SECURE",
    "AI_ENABLED",
    "LOCAL_DATA_DIR",
    "STORAGE_BACKEND",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_STORAGE_BUCKET",
    "OBJECT_SIGNING_KEY",
    "STORAGE_TIMEOUT_SECONDS",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "DBT_SEND_ANONYMOUS_USAGE_STATS",
    "MERIDIAN_DBT_THREADS",
    "MERIDIAN_DUCKDB_MEMORY_LIMIT",
    "ANALYTICS_DBT_TIMEOUT_SECONDS",
    "PGSSLROOTCERT",
    "RENDER_GIT_COMMIT",
}


def runtime_environment(env: dict[str, str], kind: str) -> dict[str, str]:
    if kind not in {"api", "outbox", "analytics"}:
        raise ValueError("Unknown Meridian runtime")
    key = "DATABASE_URL" if kind == "api" else "WORKER_DATABASE_URL"
    url = env.get(key, "")
    if not url:
        raise RuntimeError(f"{key} is required")
    try:
        parsed = make_url(url)
    except Exception:
        raise RuntimeError("Invalid runtime database configuration") from None
    expected = "meridian_app" if kind == "api" else "meridian_worker"
    if (
        parsed.drivername != "postgresql+psycopg"
        or (parsed.username or "").split(".")[0] != expected
        or parsed.query.get("sslmode") != "verify-full"
        or not parsed.password
        or not parsed.host
    ):
        raise RuntimeError("Hosted runtime requires its dedicated TLS database credential")
    result = {key: value for key, value in env.items() if key in COMMON}
    result["DATABASE_URL"] = url
    result["RUNTIME_KIND"] = "api" if kind == "api" else "worker"
    if kind != "api":
        result["WORKER_DATABASE_URL"] = url
    return result


class Supervisor:
    def __init__(self) -> None:
        self.children: list[subprocess.Popen[bytes]] = []
        self.stopping = False

    def request_stop(self, _signal: int, _frame: object) -> None:
        self.stopping = True

    def start(self, command: list[str], env: dict[str, str]) -> None:
        self.children.append(subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True))

    def stop(self, grace_seconds: float = 15.0) -> None:
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_seconds
        while any(child.poll() is None for child in self.children) and time.monotonic() < deadline:
            time.sleep(0.02)
        for child in self.children:
            # Kill the whole group, including an analytics subprocess that
            # outlived its parent. Never leave dbt working after service exit.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()

    def watch(self) -> int:
        try:
            while not self.stopping:
                if any(child.poll() is not None for child in self.children):
                    print("meridian_runtime_child_exited", file=sys.stderr, flush=True)
                    return 1
                time.sleep(0.1)
            return 0
        finally:
            self.stop()


def main() -> int:
    from meridian.config import Settings
    from meridian.object_store import ensure_key

    config = Settings(_env_file=None)
    if config.app_env in {"local", "test"}:
        raise RuntimeError("The cohosted supervisor is for the hosted synthetic demo")
    ensure_key()
    environments = {
        kind: runtime_environment(dict(os.environ), kind) for kind in ("api", "outbox", "analytics")
    }
    for environment in environments.values():
        Settings(_env_file=None, **{key.lower(): value for key, value in environment.items()})
    port = int(os.environ.get("PORT", "10000"))
    if not 1 <= port <= 65535:
        raise RuntimeError("Invalid API listening port")
    supervisor = Supervisor()
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    commands = {
        "api": [
            sys.executable,
            "-m",
            "uvicorn",
            "meridian.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--no-access-log",
        ],
        "outbox": [sys.executable, "-m", "apps.worker.run"],
        "analytics": [sys.executable, "-m", "apps.worker.analytics"],
    }
    try:
        for kind, command in commands.items():
            supervisor.start(command, environments[kind])
    except Exception:
        supervisor.stop()
        raise
    return supervisor.watch()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("meridian_runtime_start_failed", file=sys.stderr)
        raise SystemExit(1) from None
