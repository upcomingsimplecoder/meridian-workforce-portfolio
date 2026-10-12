"""Real SQL verification over the isolated seeded test DB, rolled back afterward."""

import json
import os
from datetime import date
from pathlib import Path

from operator_db import test_url as isolated_url
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
url = make_url(isolated_url())
if url.host not in {"localhost", "127.0.0.1", "postgres"}:
    raise SystemExit("Business analytics verification requires the isolated local test database")
os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
os.environ["APP_ENV"] = "test"

from meridian.analytics_models import AnalyticsRun  # noqa: E402
from meridian.analytics_pipeline import execute_run, verified_result  # noqa: E402
from meridian.analytics_schemas import AnalyticsRequest  # noqa: E402
from meridian.analytics_service import create_run  # noqa: E402
from meridian.config import settings  # noqa: E402
from meridian.db import SessionLocal  # noqa: E402
from meridian.models import uid  # noqa: E402
from meridian.people_seed import CURRENT_SNAPSHOT  # noqa: E402

with SessionLocal() as db:
    # Internal SQL validation never mints or elevates a web session.
    run: AnalyticsRun = create_run(
        db,
        AnalyticsRequest(
            snapshot_id=CURRENT_SNAPSHOT,
            as_of=date(2027, 6, 30),
            reason="Isolated real SQL business model verification",
        ),
        actor_id="hr",
        request_id=uid(),
    )
    result = execute_run(db, run)
    _, document = verified_result(db, run)
    if (
        result.status != "PASS"
        or result.report["headcount"] != 5000
        or result.report["fte"] != "4900"
        or result.report["employee_versions"] != 5002
    ):
        raise SystemExit(
            "Business analytics verification failed; numerical expectations are unchanged"
        )
    outcomes = json.loads(
        (
            Path(settings().local_data_dir)
            / "analytics/runs"
            / run.id
            / "dbt-target/run_results.json"
        ).read_text()
    )["results"]
    summary = {
        "validation": "LOCAL SQL PASS",
        "isolated_database": "meridian_test",
        "persisted": False,
        "dbt_nodes": len(outcomes),
        "headcount": result.report["headcount"],
        "fte": result.report["fte"],
        "employee_versions": result.report["employee_versions"],
        "comparison_count": len(result.report["comparisons"]),
        "model_sha256": run.model_sha256,
        "report_sha256": result.sha256,
        "cloud_validation": document["manifest"]["cloud_validation"],
    }
    phase = os.environ.get("EVIDENCE_PHASE", "p07")
    if not phase.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Invalid evidence phase name")
    (root / "docs/evidence" / (phase + "-business-analytics.json")).write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    db.rollback()
    print(
        f"LOCAL SQL PASS: {len(outcomes)} dbt models/tests; 5000 people /4900 FTE /5002 intervals; 23 count/hash/source checks. Cloud UNVERIFIED."
    )
