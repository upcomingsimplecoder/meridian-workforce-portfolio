from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail


def problem_response(request: Request, status: int, code: str, detail: str) -> JSONResponse:
    content: dict[str, Any] = {
        "type": f"urn:meridian:problem:{code}",
        "title": code.replace("_", " ").title(),
        "status": status,
        "detail": detail,
        "instance": request.url.path if len(request.url.path) <= 512 else "/[bounded request path]",
        "request_id": getattr(request.state, "request_id", "unavailable"),
    }
    return JSONResponse(content, status_code=status, media_type="application/problem+json")
