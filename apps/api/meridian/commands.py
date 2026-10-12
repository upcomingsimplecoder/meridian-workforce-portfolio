"""Shared transaction and denial-audit boundary for typed domain commands."""

from collections.abc import Callable
from functools import wraps

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.errors import DomainError
from meridian.governance import record
from meridian.identity import Actor
from meridian.models import AuditEvent
from meridian.scenarios import atomic_command


def audited_command[**P, T](command: Callable[P, T]) -> Callable[P, T]:
    """Roll back failed commands and retain otherwise unaudited scope denials."""
    atomic = atomic_command(command)

    @wraps(command)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return atomic(*args, **kwargs)
        except DomainError as error:
            db, current, request = kwargs.get("db"), kwargs.get("current"), kwargs.get("request")
            if (
                error.status == 403
                and isinstance(db, Session)
                and isinstance(current, Actor)
                and isinstance(request, Request)
            ):
                exists = db.scalar(
                    select(AuditEvent.id)
                    .where(
                        AuditEvent.request_id == request.state.request_id,
                        AuditEvent.permission_result == "DENY",
                    )
                    .limit(1)
                )
                if exists is None:
                    record(
                        db,
                        current.principal.id,
                        "security." + error.code,
                        "route",
                        request.url.path,
                        request.state.request_id,
                        False,
                        emit=False,
                    )
                    db.commit()
            raise

    return wrapped
