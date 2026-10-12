"""Test-only barrier at a real analytics handler after its transactional job lock."""

import os
import sys
import time
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    barrier = Path(os.environ["MERIDIAN_TEST_BARRIER"]).resolve()
    if os.environ.get("MERIDIAN_TEST_CONTAINER_BARRIER") == "1":
        if os.environ.get("APP_ENV") != "local":
            raise ValueError("Container barriers require the synthetic local environment")
        allowed_root = Path(os.environ["LOCAL_DATA_DIR"]).resolve()
    else:
        allowed_root = (ROOT / ".local").resolve()
    if (
        barrier.parent != allowed_root
        or not barrier.name.startswith("analytics-barrier-")
        or not barrier.is_dir()
        or barrier.is_symlink()
    ):
        raise ValueError("Barrier must remain in the project-owned private test directory")
    allowed = {str(UUID(value)) for value in os.environ["MERIDIAN_TEST_RUNS"].split(",")}
    if len(allowed) != 2:
        raise ValueError("Exactly two real analytical runs must be bound to the barrier")
    from apps.worker import analytics as worker

    actual = worker.execute_run

    def held(db, run):
        if run.id not in allowed:
            raise ValueError("Worker selected an unrelated analytical run")
        marker = barrier / (run.id + ".locked")
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(run.id)
        deadline = time.monotonic() + 90
        while not (barrier / "release").is_file():
            if time.monotonic() > deadline:
                raise TimeoutError("Test barrier was not released")
            time.sleep(0.02)
        actual(db, run)

    worker.execute_run = held
    worker.main()


if __name__ == "__main__":
    main()
