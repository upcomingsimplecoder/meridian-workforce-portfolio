"""Compensation input adapters; never a 'latest guideline' or current hierarchy lookup."""

import json

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.compensation_eligibility import freeze_population
from meridian.compensation_engine import CycleTerms, FrozenEligibility, Guideline
from meridian.compensation_models import CompGuidelineVersion
from meridian.finance_engine import FinanceInputError
from meridian.finance_repository import load_planning_history
from meridian.people_models import PrincipalEmployee
from meridian.policy import canonical_hash


def load_guideline(db: Session, guideline_id: str) -> Guideline:
    stored = db.get(CompGuidelineVersion, guideline_id)
    if stored is None:
        raise FinanceInputError("PINNED_COMPENSATION_GUIDELINE_UNAVAILABLE")
    if stored.sha256 != canonical_hash(stored.definition):
        raise FinanceInputError("PINNED_COMPENSATION_GUIDELINE_FINGERPRINT_MISMATCH")
    try:
        # JSON-mode validation parses ISO dates, while strict mode rejects numeric coercion,
        # unknown keys, unsupported models and hidden float-valued financial assumptions.
        guideline = Guideline.model_validate_json(json.dumps(stored.definition))
    except (ValidationError, ValueError) as error:
        raise FinanceInputError("INVALID_PINNED_COMPENSATION_GUIDELINE") from error
    if (
        guideline.document() != stored.definition
        or guideline.sha256 != stored.sha256
        or guideline.effective_date != stored.effective_date
    ):
        raise FinanceInputError("PINNED_COMPENSATION_GUIDELINE_FINGERPRINT_MISMATCH")
    return guideline


def load_eligibility(
    db: Session,
    *,
    snapshot_id: str,
    guideline_id: str,
    terms: CycleTerms,
    selected_ids: tuple[str, ...],
) -> tuple[FrozenEligibility, ...]:
    _, history = load_planning_history(db, snapshot_id)
    # This adapter returns internal inputs only. Launch must also require active scoped
    # comp.propose grants for each resulting proposer before storing the frozen roster.
    bindings = tuple(
        (row.employee_id, row.principal_id)
        for row in db.scalars(select(PrincipalEmployee).order_by(PrincipalEmployee.employee_id))
    )
    return freeze_population(
        history, selected_ids, bindings, load_guideline(db, guideline_id), terms
    )
