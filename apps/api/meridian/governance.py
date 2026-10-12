from typing import Any

from sqlalchemy.orm import Session

from meridian.models import AuditEvent, OutboxEvent, uid
from meridian.policy import POLICY_ID, canonical_hash
from meridian.telemetry import pin_event


def record(
    db: Session,
    actor_id: str | None,
    verb: str,
    entity_type: str,
    entity_id: str,
    request_id: str,
    allowed: bool = True,
    before_hash: str | None = None,
    after_hash: str | None = None,
    emit: bool = True,
    entity_version: int = 1,
    event_payload: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor_id,
        entity_version=entity_version,
        verb=verb,
        entity_type=entity_type,
        entity_id=canonical_hash({"route": entity_id}) if entity_type == "route" else entity_id,
        request_id=request_id,
        permission_result="ALLOW" if allowed else "DENY",
        policy_version=POLICY_ID,
        before_hash=before_hash,
        after_hash=after_hash,
    )
    db.add(event)
    if emit and allowed:
        outbox = OutboxEvent(
            id=uid(),
            event_type=f"{verb}.v1",
            aggregate_id=entity_id,
            aggregate_version=entity_version,
            request_id=request_id,
            payload=event_payload
            if event_payload is not None
            else {"entity_type": entity_type, "entity_id": entity_id},
        )
        db.add(outbox)
        pin_event(db, outbox.id)
    return event
