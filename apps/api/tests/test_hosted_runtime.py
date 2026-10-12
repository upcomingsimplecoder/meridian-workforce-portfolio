"""Hosted process credentials and actual subprocess shutdown behavior."""

import os
import signal
import sys
import threading
import time

import pytest

from scripts.hosted_runtime import Supervisor, runtime_environment


@pytest.fixture
def environment():
    return {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://meridian_app:test-only@db.example/meridian?sslmode=verify-full",
        "WORKER_DATABASE_URL": "postgresql+psycopg://meridian_worker:test-only@db.example/meridian?sslmode=verify-full",
        "OIDC_DATABASE_URL": "identity-secret",
        "MIGRATION_DATABASE_URL": "operator-secret",
        "OIDC_JWKS_JSON": "identity-private-signing-key",
        "OIDC_COOKIE_KEYS_JSON": "identity-cookie-key",
        "SUPABASE_SERVICE_ROLE_KEY": "storage-test-only",
        "OBJECT_SIGNING_KEY": "object-test-only",
        "ANALYTICS_DBT_TIMEOUT_SECONDS": "600",
        "API_ORIGIN": "https://api.example",
        "PATH": os.environ["PATH"],
    }


@pytest.mark.security
def test_api_and_workers_receive_separate_credentials(environment):
    api = runtime_environment(environment, "api")
    assert api["DATABASE_URL"] == environment["DATABASE_URL"]
    assert "WORKER_DATABASE_URL" not in api
    for kind in ("outbox", "analytics"):
        child = runtime_environment(environment, kind)
        assert child["DATABASE_URL"] == environment["WORKER_DATABASE_URL"]
        assert child["WORKER_DATABASE_URL"] == environment["WORKER_DATABASE_URL"]
        assert child["ANALYTICS_DBT_TIMEOUT_SECONDS"] == "600"
    for child in [api, runtime_environment(environment, "analytics")]:
        assert not set(child) & {
            "MIGRATION_DATABASE_URL",
            "OIDC_DATABASE_URL",
            "OIDC_JWKS_JSON",
            "OIDC_COOKIE_KEYS_JSON",
        }


@pytest.mark.security
@pytest.mark.parametrize("kind", ["outbox", "analytics"])
def test_worker_cannot_fall_back_to_api_configuration(environment, kind):
    del environment["WORKER_DATABASE_URL"]
    with pytest.raises(RuntimeError, match="WORKER_DATABASE_URL is required"):
        runtime_environment(environment, kind)


@pytest.mark.security
@pytest.mark.parametrize("change", ["api-role", "operator-role", "no-tls", "malformed"])
def test_hosted_worker_rejects_wrong_role_or_connection(environment, change):
    url = environment["WORKER_DATABASE_URL"]
    environment["WORKER_DATABASE_URL"] = {
        "api-role": environment["DATABASE_URL"],
        "operator-role": url.replace("meridian_worker", "postgres"),
        "no-tls": url.replace("verify-full", "require"),
        "malformed": "private-invalid-value",
    }[change]
    with pytest.raises(RuntimeError) as error:
        runtime_environment(environment, "analytics")
    assert "private-invalid-value" not in str(error.value)
    assert "test-only" not in str(error.value)


def test_real_worker_exit_stops_sibling_processes():
    supervisor = Supervisor()
    supervisor.start([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    supervisor.start([sys.executable, "-c", "raise SystemExit(7)"], dict(os.environ))
    assert supervisor.watch() == 1
    assert supervisor.children[0].returncode == -signal.SIGTERM
    assert supervisor.children[1].returncode == 7


def test_real_termination_stops_all_required_processes():
    supervisor = Supervisor()
    for _ in range(3):
        supervisor.start([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    stopper = threading.Timer(0.1, supervisor.request_stop, args=(signal.SIGTERM, None))
    stopper.start()
    try:
        assert supervisor.watch() == 0
        assert all(child.returncode == -signal.SIGTERM for child in supervisor.children)
    finally:
        stopper.join()
        supervisor.stop()


def test_uncooperative_child_is_killed_after_bounded_grace(tmp_path):
    marker = tmp_path / "ready"
    supervisor = Supervisor()
    supervisor.start(
        [
            sys.executable,
            "-c",
            "import signal,time,pathlib,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).touch(); time.sleep(30)",
            str(marker),
        ],
        dict(os.environ),
    )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert marker.exists()
        start = time.monotonic()
        supervisor.stop(grace_seconds=0.1)
        assert time.monotonic() - start < 2
        assert supervisor.children[0].returncode == -signal.SIGKILL
    finally:
        supervisor.stop()
