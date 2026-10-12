"""Real private-file crash recovery through the independent PostgreSQL consumer."""

from datetime import timedelta

from meridian import analytics_pipeline
from meridian.analytics_models import AnalyticsArtifact, AnalyticsResult, AnalyticsRun
from meridian.analytics_service import event_for
from meridian.models import ConsumerReceipt, now
from sqlalchemy import func, select
from test_analytics_api import analytics_worker as analytics_worker
from test_analytics_api import get, start
from test_analytics_pipeline import analytics_storage as analytics_storage


def test_crash_after_catalog_file_rolls_back_sql_receipts_then_safely_replays(
    client, login, db, analytics_worker, monkeypatch
):
    login()
    response = start(client)
    assert response.status_code == 201
    run = response.json()
    original = analytics_pipeline.put_private
    fault = True

    def interrupted(data, **kwargs):
        nonlocal fault
        key = original(data, **kwargs)
        if fault and kwargs.get("max_bytes") == analytics_pipeline.MAX_CATALOG_BYTES:
            fault = False
            raise OSError("Synthetic process interruption after catalog bytes exist")
        return key

    monkeypatch.setattr(analytics_pipeline, "put_private", interrupted)
    assert analytics_worker() == 0
    model = db.get(AnalyticsRun, run["id"])
    event = event_for(db, model)
    assert event.attempts == 1 and event.state == "PENDING"
    assert db.get(AnalyticsResult, model.id) is None
    assert db.get(ConsumerReceipt, ("analytics-v1", event.id)) is None
    assert (
        db.scalar(
            select(func.count())
            .select_from(AnalyticsArtifact)
            .where(AnalyticsArtifact.run_id == model.id)
        )
        == 0
    )
    event.next_attempt_at = now() - timedelta(seconds=1)
    db.commit()
    assert analytics_worker() == 1
    assert get(client, run)["status"] == "PASS"
    assert (
        db.scalar(
            select(func.count())
            .select_from(AnalyticsArtifact)
            .where(AnalyticsArtifact.run_id == model.id)
        )
        == 10
    )
    assert db.get(ConsumerReceipt, ("analytics-v1", event.id))
