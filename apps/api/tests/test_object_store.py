"""Supabase REST contract checks; credentialed cloud tests are a separate gate."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from meridian import object_store
from meridian.config import settings
from pydantic import SecretStr


@pytest.fixture
def hosted_storage(monkeypatch, tmp_path):
    cfg = settings()
    for name, value in {
        "storage_backend": "supabase",
        "supabase_url": "https://project.supabase.co",
        "supabase_service_role_key": SecretStr("server-only-test-key"),
        "supabase_storage_bucket": "meridian-artifacts",
        "object_signing_key": SecretStr("11" * 32),
        "local_data_dir": str(tmp_path),
    }.items():
        monkeypatch.setattr(cfg, name, value)
    remote = SimpleNamespace(
        objects={},
        writes=0,
        reads=0,
        duplicate_status=409,
        outage=False,
        lose_write_ack=False,
        chunked=False,
        bucket={
            "id": "meridian-artifacts",
            "public": False,
            "file_size_limit": object_store.MAX_OBJECT_BYTES,
            "allowed_mime_types": ["application/octet-stream"],
        },
    )

    def handler(request):
        assert request.url.host == "project.supabase.co"
        assert request.headers["authorization"] == "Bearer server-only-test-key"
        assert request.headers["apikey"] == "server-only-test-key"
        if remote.outage:
            raise httpx.ConnectError("synthetic network failure", request=request)
        if request.url.path == "/storage/v1/bucket/meridian-artifacts":
            return httpx.Response(200, json=remote.bucket)
        key = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path == "/storage/v1/object/meridian-artifacts/" + key
        if request.method == "POST":
            assert request.headers["x-upsert"] == "false"
            assert request.headers["content-type"] == "application/octet-stream"
            remote.writes += 1
            assert key == hashlib.sha256(request.content).hexdigest()
            if key in remote.objects:
                return httpx.Response(
                    remote.duplicate_status,
                    json={"code": "KeyAlreadyExists", "error": "Duplicate"},
                )
            remote.objects[key] = request.content
            if remote.lose_write_ack:
                remote.lose_write_ack = False
                raise httpx.ReadTimeout("synthetic lost write acknowledgement", request=request)
            return httpx.Response(201, json={"Key": "meridian-artifacts/" + key})
        assert request.method == "GET"
        remote.reads += 1
        if key not in remote.objects:
            return httpx.Response(404, json={"code": "NoSuchKey"})
        data = remote.objects[key]
        if remote.chunked:

            class Chunks(httpx.SyncByteStream):
                def __iter__(self):
                    yield data[:4]
                    yield data[4:]

            return httpx.Response(200, stream=Chunks())
        return httpx.Response(200, content=data)

    actual = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        object_store.httpx, "Client", lambda **kwargs: actual(transport=transport, **kwargs)
    )
    return remote


@pytest.mark.parametrize("duplicate_status", [400, 409])
def test_content_address_duplicate_race_and_process_local_files(hosted_storage, duplicate_status):
    remote = hosted_storage
    remote.duplicate_status = duplicate_status
    payload = b"synthetic private artifact"
    key = object_store.put_private(payload)
    assert key == hashlib.sha256(payload).hexdigest()
    assert object_store.put_private(payload) == key
    assert len(remote.objects) == 1 and remote.writes == 2
    assert object_store.read_private(key) == payload
    with object_store.materialize_private(key) as path:
        assert path.read_bytes() == payload and path.stat().st_mode & 0o077 == 0
    assert not path.exists()
    assert not object_store.object_path(key).exists()


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    [
        {"public": True},
        {"id": "another-bucket"},
        {"file_size_limit": None},
        {"file_size_limit": object_store.MAX_OBJECT_BYTES + 1},
        {"allowed_mime_types": None},
    ],
)
def test_public_or_unbounded_bucket_fails_before_object_access(hosted_storage, change):
    hosted_storage.bucket.update(change)
    with pytest.raises(OSError, match="must be private"):
        object_store.put_private(b"private")
    with pytest.raises(OSError, match="must be private"):
        object_store.read_private("a" * 64)
    assert hosted_storage.writes == hosted_storage.reads == 0


@pytest.mark.security
def test_corruption_is_checked_on_reads_and_duplicate_writes(hosted_storage):
    payload = b"synthetic private artifact"
    key = object_store.put_private(payload)
    hosted_storage.objects[key] = b"corrupted bytes"
    with pytest.raises(OSError, match="checksum mismatch"):
        object_store.read_private(key)
    with pytest.raises(OSError, match="checksum mismatch"):
        object_store.put_private(payload)


@pytest.mark.parametrize("chunked", [False, True])
def test_download_and_upload_bounds_prevent_oversized_objects(hosted_storage, chunked):
    payload = b"x" * 12
    key = hashlib.sha256(payload).hexdigest()
    hosted_storage.objects[key] = payload
    hosted_storage.chunked = chunked
    with pytest.raises(OSError, match="exceeds limit"):
        object_store.read_private(key, max_bytes=8)
    with pytest.raises(OSError, match="exceeds its declared work limit"):
        object_store.put_private(payload, max_bytes=8)
    assert hosted_storage.writes == 0


def test_post_write_lost_ack_can_retry_without_overwriting_bytes(hosted_storage):
    hosted_storage.lose_write_ack = True
    payload = b"synthetic immutable export"
    with pytest.raises(OSError, match="write failed"):
        object_store.put_private(payload)
    retained = dict(hosted_storage.objects)
    assert len(retained) == 1
    key = object_store.put_private(payload)
    assert hosted_storage.objects == retained
    assert object_store.read_private(key) == payload


def test_outage_has_no_local_fallback(hosted_storage):
    key = object_store.put_private(b"private")
    hosted_storage.outage = True
    with pytest.raises(OSError, match="read failed"):
        object_store.read_private(key)
    with pytest.raises(OSError, match="write failed"):
        object_store.put_private(b"private")


@pytest.mark.parametrize("configured", ["", "short", "!!" + "a" * 43, "ab" * 31])
def test_invalid_persistent_signing_key_fails_closed(hosted_storage, monkeypatch, configured):
    monkeypatch.setattr(settings(), "object_signing_key", SecretStr(configured))
    with pytest.raises(RuntimeError, match="SIGNING_KEY"):
        object_store.ensure_key()


def test_hosted_signing_key_and_link_expiry(hosted_storage):
    from meridian.errors import DomainError

    object_store.ensure_key()
    assert object_store.signing_key() == bytes.fromhex("11" * 32)
    token = object_store.sign_link("hr", "export", "cycle", 1)
    object_store.verify_link(token, "hr", "export", "cycle", 1)
    with pytest.raises(DomainError):
        object_store.verify_link(token, "executive", "export", "cycle", 1)
    expired = object_store.sign_link("hr", "export", "cycle", 1, seconds=-1)
    with pytest.raises(DomainError):
        object_store.verify_link(expired, "hr", "export", "cycle", 1)
