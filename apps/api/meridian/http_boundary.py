"""Bound actual ASGI bytes before parsing or publishing; never log request values."""

import asyncio
import json
import logging
import sys
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meridian.config import settings
from meridian.errors import problem_response
from meridian.telemetry import finish_http, server_span

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_URL_BYTES = 8192
BODY_TIMEOUT_SECONDS = 15
logger = logging.getLogger("meridian.http")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class BoundaryFailure(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail


class RequestLimiter:
    """One-process local ingress limit; deploy replicas behind a shared ingress limit."""

    def __init__(self, read_limit: int = 1200, write_limit: int = 300, max_keys: int = 4096):
        if min(read_limit, write_limit, max_keys) <= 0:
            raise ValueError("Request limits must be positive")
        self.read_limit, self.write_limit, self.max_keys = read_limit, write_limit, max_keys
        self.windows: dict[tuple[str, bool], deque[float]] = {}

    def admit(self, peer: str, write: bool, at: float) -> bool:
        key = (peer, write)
        if key not in self.windows and len(self.windows) >= self.max_keys:
            for expired in [key for key, rows in self.windows.items() if rows[-1] <= at - 60]:
                del self.windows[expired]
            if len(self.windows) >= self.max_keys:
                return False
        rows = self.windows.setdefault(key, deque())
        while rows and rows[0] <= at - 60:
            rows.popleft()
        if len(rows) >= (self.write_limit if write else self.read_limit):
            return False
        rows.append(at)
        return True


class HttpBoundary:
    def __init__(self, app: ASGIApp):
        self.app = app
        self.limiter = RequestLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["request_id"] = str(uuid.uuid4())
        with server_span(scope["state"]["request_id"]):
            await self.handle(scope, receive, send)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        started = time.perf_counter()
        status = 500
        captured: list[Message] = []
        response_size = 0
        response_limit = MAX_JSON_BYTES

        async def collect(message: Message) -> None:
            nonlocal response_size, response_limit, status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = Headers(raw=message.get("headers", []))
                if headers.get("content-disposition", "").lower().startswith("attachment;"):
                    response_limit = MAX_ARTIFACT_BYTES
                length = headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > response_limit):
                    raise BoundaryFailure(
                        503,
                        "response_too_large",
                        "Response exceeded its bounded size. Inspect persisted state before retrying a command.",
                    )
                message = {**message, "headers": list(message.get("headers", []))}
                MutableHeaders(scope=message).update(self.security_headers(request))
            elif message["type"] == "http.response.body":
                response_size += len(message.get("body", b""))
                if response_size > response_limit:
                    raise BoundaryFailure(
                        503,
                        "response_too_large",
                        "Response exceeded its bounded size. Inspect persisted state before retrying a command.",
                    )
            captured.append(message)

        try:
            self.validate_request(scope, request.headers)
            peer = scope.get("client")
            if not self.limiter.admit(
                peer[0] if peer else "unavailable",
                scope["method"] not in {"GET", "HEAD", "OPTIONS"},
                time.monotonic(),
            ):
                raise BoundaryFailure(
                    429, "request_rate_limited", "Too many requests. Retry after one minute."
                )
            content = bytearray()
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        status = 499
                        return
                    if message["type"] != "http.request":
                        raise BoundaryFailure(
                            400, "invalid_request_stream", "Invalid request stream."
                        )
                    chunk = message.get("body", b"")
                    if len(content) + len(chunk) > MAX_REQUEST_BYTES:
                        raise BoundaryFailure(413, "payload_too_large", "Request exceeds 8 MiB.")
                    content.extend(chunk)
                    if not message.get("more_body", False):
                        break
            length = request.headers.get("content-length")
            if length is not None and int(length) != len(content):
                raise BoundaryFailure(
                    400,
                    "request_length_mismatch",
                    "Declared request length does not match received bytes.",
                )
            consumed = False

            async def buffered_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(content), "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, collect)
        except TimeoutError:
            status = 408
            captured.clear()
            await self.reject(
                request,
                send,
                status,
                "request_body_timeout",
                "Request body did not arrive within the work limit.",
            )
            return
        except BoundaryFailure as error:
            status = error.status
            captured.clear()
            await self.reject(request, send, status, error.code, error.detail)
            return
        except SQLAlchemyError:
            status = 503
            captured.clear()
            await self.reject(
                request,
                send,
                status,
                "database_unavailable",
                "Database is unavailable. Inspect persisted state before retrying a command.",
            )
            return
        except Exception:
            logger.error(
                json.dumps({"event": "request_failed", "request_id": request.state.request_id})
            )
            status = 500
            captured.clear()
            await self.reject(
                request,
                send,
                status,
                "internal_error",
                "Request failed. Use the request ID when reporting this error.",
            )
            return
        finally:
            route = scope.get("route")
            finish_http(getattr(route, "path", "unmatched"), status)
            logger.info(
                json.dumps(
                    {
                        "event": "request",
                        "request_id": request.state.request_id,
                        "route": getattr(route, "path", "unmatched"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
            )
        for message in captured:
            await send(message)

    @staticmethod
    def validate_request(scope: Scope, headers: Headers) -> None:
        if (
            len(scope.get("raw_path", scope["path"].encode())) + len(scope.get("query_string", b""))
            > MAX_URL_BYTES
        ):
            raise BoundaryFailure(
                414, "request_target_too_large", "Request path and query exceed the work limit."
            )
        cfg = settings()
        allowed = {
            "localhost",
            "127.0.0.1",
            urlsplit(cfg.web_origin).hostname,
            urlsplit(cfg.api_origin).hostname,
        }
        try:
            authority = urlsplit("//" + headers.get("host", ""))
            hostname = authority.hostname
            _ = authority.port
            if (
                authority.username
                or authority.password
                or authority.path
                or authority.query
                or authority.fragment
                or len(headers.getlist("host")) != 1
            ):
                hostname = None
        except ValueError:
            hostname = None
        if hostname not in allowed:
            raise BoundaryFailure(
                400, "untrusted_host", "Request host is not configured for this service."
            )
        lengths = headers.getlist("content-length")
        if len(lengths) > 1 or (lengths and headers.get("transfer-encoding")):
            raise BoundaryFailure(
                400, "ambiguous_request_length", "Ambiguous request framing is not accepted."
            )
        if lengths and not lengths[0].isdecimal():
            raise BoundaryFailure(
                400, "invalid_request_length", "Request length must be a nonnegative integer."
            )
        if lengths and int(lengths[0]) > MAX_REQUEST_BYTES:
            raise BoundaryFailure(413, "payload_too_large", "Request exceeds 8 MiB.")

    @staticmethod
    def security_headers(request: Request) -> dict[str, str]:
        headers = {
            "X-Request-ID": request.state.request_id,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        if settings().cookie_secure:
            headers["Strict-Transport-Security"] = "max-age=31536000"
        return headers

    async def reject(
        self, request: Request, send: Send, status: int, code: str, detail: str
    ) -> None:
        response = problem_response(request, status, code, detail)
        response.headers.update(self.security_headers(request))
        if status == 429:
            response.headers["Retry-After"] = "60"
        await response(request.scope, request.receive, send)
