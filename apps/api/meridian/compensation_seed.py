"""Explicit synthetic compensation limits; no employer policy or market salary claims."""

from datetime import date

from sqlalchemy.orm import Session

from meridian.compensation_engine import Guideline
from meridian.compensation_models import CompGuidelineVersion
from meridian.people_seed import stable_id

GUIDELINE_ID = stable_id("comp-guideline-synthetic-1")
DEMO_GUIDELINE = Guideline(effective_date=date(2026, 1, 1))


def seed_compensation(db: Session) -> None:
    if db.get(CompGuidelineVersion, GUIDELINE_ID) is None:
        db.add(
            CompGuidelineVersion(
                id=GUIDELINE_ID,
                source_label="Synthetic merit and bonus guideline v1",
                effective_date=DEMO_GUIDELINE.effective_date,
                definition=DEMO_GUIDELINE.document(),
                sha256=DEMO_GUIDELINE.sha256,
            )
        )
