"""Private content-addressed storage and principal-bound, expiring object links.

Local development uses a private filesystem adapter. Hosted services use the
Supabase Storage REST API through this backend only. The browser never receives
the service-role key or a direct unrestricted storage URL.
"""

import hashlib
import os
import re
import secrets
import tempfile
import time
from base64 import b64decode
from collections.abc import Generator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from meridian.config import settings
from meridian.errors import DomainError
from meridian.models import now

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 8192


def ensure_key() -> None:
    cfg = settings()
    if cfg.storage_backend == "supabase":
        _hosted_signing_key()
        return
    if cfg.app_env not in {"local", "test"}:
        raise RuntimeError("Configure a reviewed hosted object store before enabling ingestion")
    folder = Path(cfg.local_data_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = folder / "object-signing.key"
    try:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(secrets.token_bytes(32))
        stream.flush()
        os.fsync(stream.fileno())


def signing_key() -> bytes:
    if settings().storage_backend == "supabase":
        return _hosted_signing_key()
    key = (Path(settings().local_data_dir) / "object-signing.key").read_bytes()
    if len(key) != 32:
        raise RuntimeError("Invalid local signing key")
    return key


def _hosted_signing_key() -> bytes:
    configured = settings().object_signing_key.get_secret_value().strip()
    if not configured:
        raise RuntimeError("OBJECT_SIGNING_KEY is required for hosted object links")
    try:
        if re.fullmatch(r"[0-9a-fA-F]{64}", configured):
            key = bytes.fromhex(configured)
        else:
            padded = configured + "=" * (-len(configured) % 4)
            key = b64decode(padded.encode(), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("OBJECT_SIGNING_KEY must encode exactly 32 bytes") from exc
    if len(key) != 32:
        raise RuntimeError("OBJECT_SIGNING_KEY must encode exactly 32 bytes")
    return key


def _supabase_headers() -> dict[str, str]:
    key = settings().supabase_service_role_key.get_secret_value()
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is required for hosted object storage")
    return {"Authorization": f"Bearer {key}", "apikey": key, "Accept-Encoding": "identity"}


def _supabase_base() -> str:
    cfg = settings()
    if not cfg.supabase_url.startswith("https://") or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", cfg.supabase_storage_bucket
    ):
        raise RuntimeError("Supabase storage is not configured")
    return cfg.supabase_url.rstrip("/") + "/storage/v1"


def _supabase_url(key: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid storage key")
    cfg = settings()
    bucket = quote(cfg.supabase_storage_bucket, safe="")
    return f"{_supabase_base()}/object/{bucket}/{key}"


def _bounded_response(response: httpx.Response, limit: int) -> bytes:
    if response.headers.get("content-encoding", "identity") != "identity":
        raise OSError("Storage response must use uncompressed transport bytes")
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > limit):
        raise OSError("Stored object exceeds limit")
    deadline = time.monotonic() + settings().storage_timeout_seconds
    content = bytearray()
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise OSError("Storage response exceeded its time limit")
        if len(content) + len(chunk) > limit:
            raise OSError("Stored object exceeds limit")
        content.extend(chunk)
    if declared is not None and len(content) != int(declared):
        raise OSError("Storage response length mismatch")
    return bytes(content)


def _require_private_bucket(client: httpx.Client) -> None:
    import json

    cfg = settings()
    url = f"{_supabase_base()}/bucket/{quote(cfg.supabase_storage_bucket, safe='')}"
    with client.stream("GET", url, headers=_supabase_headers()) as response:
        if response.status_code != 200:
            raise OSError("Private storage bucket is unavailable")
        try:
            bucket = json.loads(_bounded_response(response, MAX_METADATA_BYTES))
        except (ValueError, TypeError) as exc:
            raise OSError("Invalid storage bucket configuration") from exc
    if (
        not isinstance(bucket, dict)
        or bucket.get("id") != cfg.supabase_storage_bucket
        or bucket.get("public") is not False
        or type(bucket.get("file_size_limit")) is not int
        or not 0 < bucket["file_size_limit"] <= MAX_OBJECT_BYTES
        or bucket.get("allowed_mime_types") != ["application/octet-stream"]
    ):
        raise OSError("Storage bucket must be private with explicit size and content limits")


def _supabase_read(key: str, *, max_bytes: int) -> bytes:
    try:
        with httpx.Client(timeout=settings().storage_timeout_seconds) as client:
            _require_private_bucket(client)
            with client.stream("GET", _supabase_url(key), headers=_supabase_headers()) as response:
                if response.status_code != 200:
                    raise OSError(f"Supabase object read failed ({response.status_code})")
                data = _bounded_response(response, max_bytes)
    except (httpx.HTTPError, ValueError) as exc:
        raise OSError("Supabase object read failed") from exc
    if hashlib.sha256(data).hexdigest() != key:
        raise OSError("Stored object checksum mismatch")
    return data


def _supabase_put(key: str, data: bytes, *, max_bytes: int) -> None:
    import json

    try:
        with httpx.Client(timeout=settings().storage_timeout_seconds) as client:
            _require_private_bucket(client)
            with client.stream(
                "POST",
                _supabase_url(key),
                content=data,
                headers={
                    **_supabase_headers(),
                    "Content-Type": "application/octet-stream",
                    "x-upsert": "false",
                },
            ) as response:
                body = _bounded_response(response, MAX_METADATA_BYTES)
                if response.status_code not in {200, 201}:
                    try:
                        error = json.loads(body)
                    except (ValueError, TypeError):
                        error = {}
                    duplicate = isinstance(error, dict) and (
                        error.get("code") in {"ResourceAlreadyExists", "KeyAlreadyExists"}
                        or error.get("error") == "Duplicate"
                    )
                    if response.status_code not in {400, 409} or not duplicate:
                        raise OSError(f"Supabase object write failed ({response.status_code})")
            # A duplicate write is valid only when the existing content still
            # matches the content address. Always read back through the private
            # API before acknowledging the write.
            stored = _supabase_read(key, max_bytes=max_bytes)
    except httpx.HTTPError as exc:
        raise OSError("Supabase object write failed") from exc
    if stored != data:
        raise OSError("Stored object differs from its content address")


def sign_link(actor: str, purpose: str, resource: str, version: int, seconds: int = 300) -> str:
    return jwt.encode(
        {
            "sub": actor,
            "aud": "meridian-private-objects",
            "purpose": purpose,
            "resource": resource,
            "version": version,
            "exp": now() + timedelta(seconds=seconds),
        },
        signing_key(),
        algorithm="HS256",
    )


def verify_link(token: str, actor: str, purpose: str, resource: str, version: int) -> None:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key(),
            algorithms=["HS256"],
            audience="meridian-private-objects",
            options={"require": ["exp", "sub", "aud", "purpose", "resource", "version"]},
        )
        if any(
            claims[field] != value
            for field, value in {
                "sub": actor,
                "purpose": purpose,
                "resource": resource,
                "version": version,
            }.items()
        ):
            raise jwt.InvalidTokenError()
    except jwt.PyJWTError as exc:
        raise DomainError(
            403,
            "invalid_object_link",
            "Object link is expired, stale or unavailable to this principal.",
        ) from exc


def object_path(key: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid storage key")
    return Path(settings().local_data_dir) / "objects" / key


def _validate_limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_OBJECT_BYTES:
        raise ValueError("Object work limit must be between 1 byte and 32 MiB")


def put_private(data: bytes, *, max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    _validate_limit(max_bytes)
    if len(data) > max_bytes:
        raise OSError("Private artifact exceeds its declared work limit")
    digest = hashlib.sha256(data).hexdigest()
    if settings().storage_backend == "supabase":
        _supabase_put(digest, data, max_bytes=max_bytes)
        return digest
    target = object_path(digest)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        if read_private(digest, max_bytes=max_bytes) != data:
            raise OSError("Stored object differs from its content address")
        return digest
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if read_private(digest, max_bytes=max_bytes) != data:
                    raise OSError("Stored object differs from its content address") from None
            # Persist the directory entry as well as file contents before acknowledging storage.
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return digest


def read_private(key: str, *, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    _validate_limit(max_bytes)
    if settings().storage_backend == "supabase":
        return _supabase_read(key, max_bytes=max_bytes)
    path = object_path(key)
    if path.stat().st_size > max_bytes:
        raise OSError("Stored object exceeds limit")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != key:
        raise OSError("Stored object checksum mismatch")
    return data


@contextmanager
def materialize_private(key: str, *, max_bytes: int = MAX_UPLOAD_BYTES) -> Generator[Path]:
    """Give DuckDB a verified process-local file and remove it after the read."""
    data = read_private(key, max_bytes=max_bytes)
    if settings().storage_backend == "local":
        yield object_path(key)
        return
    with tempfile.TemporaryDirectory(prefix="meridian-private-") as directory:
        path = Path(directory) / key
        path.write_bytes(data)
        path.chmod(0o600)
        yield path
