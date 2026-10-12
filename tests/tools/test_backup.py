import hashlib
import io
import json
import os
import tarfile

import backup
import backup_crypto as crypto
import pytest
from cryptography.exceptions import InvalidTag


def private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("size", [0, 1, crypto.CHUNK + 31])
def test_streaming_roundtrip_nonce_and_private_permissions(tmp_path, size):
    source = private(tmp_path / "source", os.urandom(size))
    key = os.urandom(32)
    sealed, other, restored = (tmp_path / name for name in ("sealed", "other", "restored"))
    crypto.encrypt_file(source, sealed, key)
    crypto.encrypt_file(source, other, key)
    assert sealed.read_bytes() != other.read_bytes()
    assert sealed.stat().st_mode & 0o777 == 0o600
    crypto.decrypt_file(sealed, restored, key)
    assert restored.read_bytes() == source.read_bytes()
    assert restored.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("corruption", ["header", "nonce", "body", "tag", "truncated", "wrong-key"])
def test_tamper_fails_and_removes_every_plaintext_byte(tmp_path, corruption):
    source = private(tmp_path / "source", b"synthetic-source" * 100)
    sealed, restored = tmp_path / "sealed", tmp_path / "restored"
    key = os.urandom(32)
    crypto.encrypt_file(source, sealed, key)
    data = bytearray(sealed.read_bytes())
    if corruption == "truncated":
        data = data[: len(crypto.MAGIC) + 10]
    elif corruption != "wrong-key":
        index = {
            "header": 0,
            "nonce": len(crypto.MAGIC),
            "body": len(crypto.MAGIC) + 13,
            "tag": -1,
        }[corruption]
        data[index] ^= 1
    else:
        key = os.urandom(32)
    sealed.write_bytes(data)
    with pytest.raises((ValueError, InvalidTag)):
        crypto.decrypt_file(sealed, restored, key)
    assert not restored.exists()


def test_crypto_never_overwrites_a_destination(tmp_path):
    source = private(tmp_path / "source", b"synthetic")
    target = private(tmp_path / "target", b"retained")
    with pytest.raises(FileExistsError):
        crypto.encrypt_file(source, target, os.urandom(32))
    assert target.read_bytes() == b"retained"


@pytest.mark.parametrize(
    "kind", ["public-source", "source-symlink", "key-symlink", "public-key", "short-key"]
)
def test_private_file_requirements(tmp_path, kind):
    source = private(tmp_path / "source", b"synthetic")
    key = private(tmp_path / "key", os.urandom(32))
    if kind == "public-source":
        source.chmod(0o644)
    elif kind == "source-symlink":
        linked = tmp_path / "linked"
        linked.symlink_to(source)
        source = linked
    elif kind == "key-symlink":
        linked = tmp_path / "linked"
        linked.symlink_to(key)
        key = linked
    elif kind == "public-key":
        key.chmod(0o644)
    else:
        key.write_bytes(b"short")
    with pytest.raises(ValueError):
        crypto.encrypt_file(source, tmp_path / "sealed", crypto.private_key(key))


def test_oversized_crypto_input_rejected_before_destination(tmp_path, monkeypatch):
    source = private(tmp_path / "source", b"too-long")
    monkeypatch.setattr(crypto, "MAX_ARCHIVE", 4)
    with pytest.raises(ValueError):
        crypto.encrypt_file(source, tmp_path / "sealed", os.urandom(32))
    assert not (tmp_path / "sealed").exists()


def archive(path, *, malicious=None, mutate_manifest=None):
    value = b"synthetic-object"
    digest = hashlib.sha256(value).hexdigest()
    files = {"database.dump": b"synthetic-not-a-postgres-dump", "objects/" + digest: value}
    files.update({"private/" + name: b"synthetic-private-key" for name in backup.KEY_NAMES})
    manifest = {
        "format": "meridian-native-backup-v1",
        "created_at": "2026-10-10T00:00:00+00:00",
        "table_counts": {"audit_event": 12},
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        },
    }
    if mutate_manifest:
        mutate_manifest(manifest)
    files["manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(path, "w") as target:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))
        if malicious:
            info, data = malicious
            if info.isfile():
                info.size = len(data)
            target.addfile(info, io.BytesIO(data) if info.isfile() else None)
    path.chmod(0o600)
    return manifest


def test_exact_archive_inventory_and_streamed_hashes(tmp_path):
    source = tmp_path / "archive.tar"
    expected = archive(source)
    destination = tmp_path / "data"
    destination.mkdir(mode=0o700)
    assert backup.unpack_authenticated(source, destination) == expected
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.rglob("*") if p.is_file())


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "duplicate", "extra"])
def test_unsafe_archive_members_rejected_without_extracting(tmp_path, kind):
    name = {
        "traversal": "../outside",
        "absolute": "/tmp/meridian-outside",
        "symlink": "database.dump",
        "duplicate": "database.dump",
        "extra": "private/unapproved-key",
    }[kind]
    info = tarfile.TarInfo(name)
    if kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = "../outside"
    source = tmp_path / "archive.tar"
    archive(source, malicious=(info, b"bad"))
    destination = tmp_path / "data"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        backup.unpack_authenticated(source, destination)
    assert list(destination.iterdir()) == []
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("field", ["sha256", "bytes", "table_counts", "format", "missing-private"])
def test_manifest_mismatch_rejected_before_restore(tmp_path, field):
    def change(document):
        if field in {"sha256", "bytes"}:
            document["files"]["database.dump"][field] = "0" * 64 if field == "sha256" else 0
        elif field == "table_counts":
            document[field] = {"audit_event; DROP SCHEMA public": 12}
        elif field == "missing-private":
            document["files"].pop("private/object-signing.key")
        else:
            document[field] = "untrusted"

    source = tmp_path / "archive.tar"
    archive(source, mutate_manifest=change)
    destination = tmp_path / "data"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        backup.unpack_authenticated(source, destination)


@pytest.mark.parametrize("kind", ["tag", "inventory"])
def test_no_database_connection_before_authenticated_complete_inventory(
    tmp_path, monkeypatch, kind
):
    source = tmp_path / "archive.tar"
    archive(
        source,
        mutate_manifest=(lambda x: x.update(format="untrusted")) if kind == "inventory" else None,
    )
    key = private(tmp_path / "key", os.urandom(32))
    sealed = tmp_path / "sealed"
    crypto.encrypt_file(source, sealed, key.read_bytes())
    if kind == "tag":
        data = bytearray(sealed.read_bytes())
        data[-1] ^= 1
        sealed.write_bytes(data)
    monkeypatch.setattr(backup, "LOCAL", tmp_path)
    monkeypatch.setattr(backup, "source_url", lambda: None)
    connections = []
    monkeypatch.setattr(backup.psycopg, "connect", lambda *a, **kw: connections.append((a, kw)))
    with pytest.raises((ValueError, InvalidTag)):
        backup.rehearse(sealed, key)
    assert connections == []
    assert list((tmp_path / "restores").iterdir()) == []


def test_retention_keeps_three_newest_and_rejects_unexpected_paths(tmp_path):
    now = 1791633600
    for day in range(1, 6):
        path = private(tmp_path / f"2026100{day}T000000Z-{'a' * 31}{day}.aesgcm", b"encrypted")
        os.utime(path, (now - (6 - day) * 86400, now - (6 - day) * 86400))
    assert backup.apply_retention(tmp_path, now=now) == 3
    names = sorted(p.name for p in tmp_path.iterdir())
    assert [name[7] for name in names] == ["3", "4", "5"]
    unexpected = private(tmp_path / "user-file", b"preserve")
    with pytest.raises(ValueError):
        backup.apply_retention(tmp_path, now=now + 30 * 86400)
    assert unexpected.read_bytes() == b"preserve"
    assert len(list(tmp_path.iterdir())) == 4
    unexpected.unlink()
    assert backup.apply_retention(tmp_path, now=now + 30 * 86400) == 0
