"""Stop/restart only the project native PG cluster; verify real browser degradation."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from zipfile import ZipFile

import psycopg
from operator_db import admin_url
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = ROOT / ".local/postgres"


def pins(url):
    with psycopg.connect(url) as db:
        counts = {
            name: db.execute(
                psycopg.sql.SQL("SELECT count(*) FROM {}").format(psycopg.sql.Identifier(name))
            ).fetchone()[0]
            for name in (
                "employee",
                "employee_version",
                "snapshot",
                "scenario_revision",
                "comp_proposal_revision",
            )
        }
        capsules = db.execute(
            "SELECT id,sha256 FROM replay_capsule ORDER BY id LIMIT 10001"
        ).fetchall()
        if len(capsules) > 10000:
            raise RuntimeError("Native pin inventory exceeds drill bound")
        return {
            "counts": counts,
            "all_existing_capsules_sha256": hashlib.sha256(
                json.dumps(capsules).encode()
            ).hexdigest(),
            "capsules": len(capsules),
        }


def wait_file(path, child):
    until = time.monotonic() + 45
    while not path.exists() and child.poll() is None and time.monotonic() < until:
        time.sleep(0.02)
    if not path.exists():
        raise RuntimeError("Actual browser drill barrier not observed")


def main():
    url = make_url(admin_url())
    if (
        url.host not in {"127.0.0.1", "localhost"}
        or url.port != 54329
        or url.database != "meridian_demo"
    ):
        raise RuntimeError("Only the owned native demo cluster may be stopped")
    if CLUSTER.is_symlink() or (CLUSTER / "PG_VERSION").read_text().strip() != "17":
        raise RuntimeError("Unexpected native cluster")
    postmaster = (CLUSTER / "postmaster.pid").read_text().splitlines()
    if Path(postmaster[1]).resolve() != CLUSTER.resolve() or postmaster[3] != "54329":
        raise RuntimeError("Cluster data directory and port ownership mismatch")
    binary = Path("/opt/homebrew/opt/postgresql@17/bin/pg_ctl")
    if not binary.is_file():
        raise RuntimeError("Verified native PostgreSQL binary unavailable")
    uri = url.set(drivername="postgresql").render_as_string(hide_password=False)
    before = pins(uri)
    api_pid = int((ROOT / ".local/api.pid").read_text())
    identity_pid = int((ROOT / ".local/identity.pid").read_text())
    os.kill(api_pid, 0)
    os.kill(identity_pid, 0)
    start = time.perf_counter()
    stopped = False

    def restart():
        subprocess.run(
            [
                str(binary),
                "-D",
                str(CLUSTER),
                "-l",
                str(ROOT / ".local/postgres.log"),
                "-w",
                "-t",
                "30",
                "-o",
                "-p 54329 -h 127.0.0.1",
                "start",
            ],
            check=True,
            capture_output=True,
            timeout=35,
        )

    with tempfile.TemporaryDirectory(prefix="db-reconnect-", dir=ROOT / ".local") as directory:
        directory = Path(directory)
        env = os.environ | {"PATH": "/opt/homebrew/opt/node@24/bin:" + os.environ["PATH"]}
        with (directory / "browser.log").open("wb") as log:
            child = subprocess.Popen(
                ["node", "scripts/reconnect_browser.mjs", str(directory)],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                wait_file(directory / "browser-ready", child)
                subprocess.run(
                    [str(binary), "-D", str(CLUSTER), "-m", "fast", "-w", "-t", "30", "stop"],
                    check=True,
                    capture_output=True,
                    timeout=35,
                )
                stopped = True
                (directory / "database-stopped").write_text("stopped")
                wait_file(directory / "outage-observed.json", child)
                outage = json.loads((directory / "outage-observed.json").read_text())
                restart()
                stopped = False
                (directory / "database-started").write_text("started")
                wait_file(directory / "recovered.json", child)
                if child.wait(timeout=20) != 0:
                    raise RuntimeError("Actual browser recovery failed")
                recovered = json.loads((directory / "recovered.json").read_text())
            finally:
                if stopped:
                    restart()
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=20)
                (ROOT / ".local/p10-db-reconnect-browser.log").write_bytes(
                    (directory / "browser.log").read_bytes()
                )
    if api_pid != int((ROOT / ".local/api.pid").read_text()):
        raise RuntimeError("API process was replaced")
    os.kill(api_pid, 0)
    if identity_pid != int((ROOT / ".local/identity.pid").read_text()):
        raise RuntimeError("Identity process was replaced")
    os.kill(identity_pid, 0)
    after = pins(uri)
    if after != before:
        raise RuntimeError("Immutable source/pin inventory changed during reconnect")
    trace = ROOT / ".local/evidence/p10-db-reconnect/trace.zip"
    with ZipFile(trace) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Private reconnect trace invalid")
        entries = len(archive.namelist())
    report = {
        "scope": "Actual owned native PG fast stop/start and same API process/session recovery; Chromium browser only; not hosted outage or all-engine reconnect",
        "outage": outage,
        "recovery": recovered,
        "same_api_process": True,
        "same_identity_process": True,
        "source_pins_preserved": before,
        "seconds": round(time.perf_counter() - start, 3),
        "private_trace": {
            "sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "entries": entries,
            "crc_valid": True,
        },
    }
    (ROOT / "docs/evidence/p10-db-reconnect.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
