"""Sealed synthetic qualitative policy; no ratings, predictions or ranking model."""

from typing import Any

from sqlalchemy.orm import Session

from meridian.people_seed import stable_id
from meridian.policy import canonical_hash
from meridian.talent_models import TalentPolicy

TALENT_POLICY_ID = stable_id("talent-narrative-policy-v1")
TALENT_POLICY: dict[str, Any] = {
    "model_version": "human-narrative-context-v1",
    "required_sections": ["accomplishments", "collaboration", "development"],
    "minimum_section_characters": 10,
    "maximum_section_characters": 2000,
    "evidence_kind": "WORKFORCE_CONTEXT",
    "evidence_semantics": "synthetic human observation with pinned workforce context; not independently validated performance evidence",
    "minimum_evidence_references": 1,
    "maximum_evidence_references": 10,
    "maximum_packets": 500,
    "maximum_revisions": 101,
    "maximum_export_bytes": 8388608,
    "maximum_cycle_comments": 2000,
    "signature_route": ["ASSIGNED_CALIBRATOR", "ASSIGNED_HR"],
    "professional_human_policy_review_required": True,
    "ai_scoring_enabled": False,
}


def seed_talent(db: Session) -> None:
    if db.get(TalentPolicy, TALENT_POLICY_ID) is None:
        db.add(
            TalentPolicy(
                id=TALENT_POLICY_ID,
                title="Synthetic human narrative and calibration v1",
                definition=TALENT_POLICY,
                sha256=canonical_hash(TALENT_POLICY),
            )
        )
