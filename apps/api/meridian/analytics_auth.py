"""Full curated analytics and separate executive/auditor projections."""

from fastapi import Request
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.governance import record
from meridian.identity import Actor, scopes


def authorize(current: Actor, capability: str, db: Session, request: Request) -> None:
    if "*" not in scopes(current, capability):
        record(
            db,
            current.principal.id,
            "security.analytics_denied",
            "route",
            request.url.path,
            request.state.request_id,
            False,
            emit=False,
        )
        db.commit()
        raise DomainError(
            403,
            "analytics_forbidden",
            "Your current scoped grants do not permit this analytical view or action.",
        )
