"""Commit liveness separately so a heartbeat row cannot serialize long job locks."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from meridian.models import WorkerHeartbeat, now


def update_heartbeat(db: Session, name: str) -> None:
    timestamp = now()
    statement = insert(WorkerHeartbeat).values(name=name, last_seen_at=timestamp)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.name], set_={"last_seen_at": timestamp}
        )
    )
