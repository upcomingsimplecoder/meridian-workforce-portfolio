from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.ingestion_models import IdempotencyReceipt
from meridian.policy import canonical_hash


def lock_key(db: Session, namespace: str) -> None:
    key = int(canonical_hash(namespace)[:15], 16)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def replay(db: Session, actor: str, route: str, key: str, payload: Any) -> dict[str, Any] | None:
    lock_key(db, f"{actor}:{route}:{key}")
    receipt = db.scalar(
        select(IdempotencyReceipt).where(
            IdempotencyReceipt.actor_id == actor,
            IdempotencyReceipt.route == route,
            IdempotencyReceipt.key == key,
        )
    )
    if receipt is None:
        return None
    if receipt.request_sha256 != canonical_hash(payload):
        raise DomainError(
            409, "idempotency_conflict", "This idempotency key was used for different input."
        )
    return receipt.response


def remember(
    db: Session,
    actor: str,
    route: str,
    key: str,
    payload: Any,
    response: dict[str, Any],
    request_id: str,
) -> None:
    db.add(
        IdempotencyReceipt(
            actor_id=actor,
            route=route,
            key=key,
            request_sha256=canonical_hash(payload),
            response=response,
            request_payload=payload,
            request_id=request_id,
        )
    )
