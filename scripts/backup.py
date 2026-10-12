"""Native encrypted backup and isolated restore rehearsal; never resets the demo."""

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from backup_crypto import MAX_ARCHIVE, decrypt_file, encrypt_file, new_private, private_key
from operator_db import admin_url
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
KEY_NAMES = {
    "object-signing.key",
    "oidc-keys.json",
    "oidc-cookie-keys.json",
    "demo-credentials.json",
}
ARCHIVE_NAME = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{32}\.aesgcm")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require_quiescent():
    """Back up the native demo only after its owned application processes stop."""
    for name in ("api", "identity", "worker", "analytics-worker", "web"):
        path = LOCAL / f"{name}.pid"
        if path.exists():
            try:
                os.kill(int(path.read_text()), 0)
            except ProcessLookupError:
                continue
            raise ValueError("Stop owned Meridian application processes before backing up")


def apply_retention(directory, now=None):
    """At most three project-format archives, no archive older than seven days."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Retention requires a regular backup directory")
    entries = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or not ARCHIVE_NAME.fullmatch(path.name):
            raise ValueError("Refusing retention in a directory with unexpected files")
        entries.append(path)
    moment = time.time() if now is None else now
    retained = []
    for path in sorted(entries, key=lambda row: row.name, reverse=True):
        if moment - path.stat().st_mtime > 7 * 86400 or len(retained) >= 3:
            path.unlink()
        else:
            retained.append(path)
    return len(retained)


def pg_bin():
    configured = os.environ.get("MERIDIAN_PG_BIN")
    if configured:
        return Path(configured)
    homebrew = Path("/opt/homebrew/opt/postgresql@17/bin")
    if homebrew.exists():
        return homebrew
    found = shutil.which("pg_dump")
    if found is None:
        raise ValueError("PostgreSQL 17 backup binaries are required")
    return Path(found).parent


def source_url():
    url = make_url(admin_url())
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "meridian_demo":
        raise ValueError("This native rehearsal permits only the project-owned loopback demo")
    return url


def pg_env(url):
    return os.environ | {
        "PGHOST": url.host,
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }


def counts(url):
    uri = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(uri) as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
        ]
        return {
            table: db.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", table))
            ).fetchone()[0]
            for table in tables
        }


def backup(key_path):
    url = source_url()
    require_quiescent()
    target = LOCAL / "backups"
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink():
        raise ValueError("Backup directory cannot be a symlink")
    if not key_path.exists():
        with new_private(key_path) as handle:
            handle.write(secrets.token_bytes(32))
    key = private_key(key_path)
    before = counts(url)
    with tempfile.TemporaryDirectory(prefix="backup-", dir=LOCAL) as temporary:
        directory = Path(temporary)
        dump = directory / "database.dump"
        subprocess.run(
            [
                str(pg_bin() / "pg_dump"),
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(dump),
            ],
            env=pg_env(url),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        dump.chmod(0o600)
        if counts(url) != before:
            raise ValueError("Source changed during backup; stop owned app processes and retry")
        object_directory = LOCAL / "objects"
        if object_directory.is_symlink() or not object_directory.is_dir():
            raise ValueError("Private object directory is unavailable")
        objects = sorted(object_directory.iterdir())
        files = {"database.dump": dump}
        hashes = {}
        for path in objects:
            if (
                path.is_symlink()
                or not path.is_file()
                or not re.fullmatch(r"[0-9a-f]{64}", path.name)
            ):
                raise ValueError("Object inventory contains an unexpected path")
            if path.stat().st_size > 32 * 1024**2 or sha256_file(path) != path.name:
                raise ValueError("Private object inventory failed checksum verification")
            files["objects/" + path.name] = path
        for name in KEY_NAMES:
            path = LOCAL / name
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError("Required private signing/identity files are unavailable")
            files["private/" + name] = path
        for name, path in files.items():
            hashes[name] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        manifest = {
            "format": "meridian-native-backup-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "table_counts": before,
            "files": hashes,
        }
        archive_path = directory / "archive.tar"
        with tarfile.open(archive_path, "w") as archive:
            for name, path in files.items():
                info = archive.gettarinfo(str(path), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o600
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            data = json.dumps(manifest, sort_keys=True).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        archive_path.chmod(0o600)
        sealed = target / (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex + ".aesgcm"
        )
        encrypt_file(archive_path, sealed, key)
        apply_retention(target)
    return sealed, manifest


def unpack_authenticated(archive_path, destination):
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Restore extraction requires an empty private directory")
    with tarfile.open(archive_path, "r:") as archive:
        members = []
        total = 0
        for member in archive:
            total += member.size
            if len(members) >= 100000 or total > MAX_ARCHIVE:
                raise ValueError("Backup inventory exceeds its bounded archive limit")
            members.append(member)
        seen = set()
        for member in members:
            allowed = (
                member.name in {"database.dump", "manifest.json"}
                or member.name.startswith("private/")
                and member.name[8:] in KEY_NAMES
                or re.fullmatch(r"objects/[0-9a-f]{64}", member.name)
            )
            if not allowed or not member.isfile() or member.name in seen or member.size < 0:
                raise ValueError("Backup contains an unsafe or duplicate archive member")
            seen.add(member.name)
            limit = (
                MAX_ARCHIVE
                if member.name == "database.dump"
                else 1024**2
                if member.name == "manifest.json"
                else 32 * 1024**2
            )
            if member.size > limit:
                raise ValueError("Backup member exceeds its declared limit")
        if (
            not {"database.dump", "manifest.json", *("private/" + name for name in KEY_NAMES)}
            <= seen
        ):
            raise ValueError("Backup is missing its required authenticated inventory")
        for member in members:
            target = destination / member.name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Expected regular backup member is unavailable")
            with new_private(target) as output:
                shutil.copyfileobj(stream, output, length=1024 * 1024)

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate manifest key")
            value[key] = item
        return value

    manifest = json.loads(
        (destination / "manifest.json").read_text(), object_pairs_hook=unique_pairs
    )
    if (
        manifest.get("format") != "meridian-native-backup-v1"
        or set(manifest) != {"format", "created_at", "table_counts", "files"}
        or not isinstance(manifest["files"], dict)
        or not isinstance(manifest["table_counts"], dict)
        or not {"database.dump", *("private/" + name for name in KEY_NAMES)}
        <= set(manifest["files"])
        or set(manifest["files"]) | {"manifest.json"} != seen
    ):
        raise ValueError("Backup inventory does not match its authenticated manifest")
    for table, count in manifest["table_counts"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", table) or type(count) is not int or count < 0:
            raise ValueError("Invalid authenticated table inventory")
    for name, expected in manifest["files"].items():
        file = destination / name
        if (
            not isinstance(expected, dict)
            or set(expected) != {"bytes", "sha256"}
            or type(expected["bytes"]) is not int
            or not isinstance(expected["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
            or file.stat().st_size != expected["bytes"]
            or sha256_file(file) != expected["sha256"]
        ):
            raise ValueError("Authenticated backup contents fail their manifest checksum")
        if name.startswith("objects/") and expected["sha256"] != name[8:]:
            raise ValueError("Authenticated object name differs from its content hash")
    return manifest


def rehearse(sealed, key_path):
    operator = source_url()
    restore_root = LOCAL / "restores"
    if restore_root.is_symlink():
        raise ValueError("Restore directory cannot be a symlink")
    target = restore_root / uuid4().hex
    target.mkdir(mode=0o700, parents=True)
    archive = target / "authenticated.tar"
    data = target / "data"
    try:
        decrypt_file(sealed, archive, private_key(key_path))
        data.mkdir(mode=0o700)
        manifest = unpack_authenticated(archive, data)
    except BaseException:
        shutil.rmtree(target)
        raise
    database = "meridian_restore_" + uuid4().hex[:16]
    restored = operator.set(database=database)
    uri = operator.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(uri, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    # Neither SQL nor filesystem restoration begins until AEAD and all manifest hashes pass.
    subprocess.run(
        [
            str(pg_bin() / "pg_restore"),
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--dbname",
            database,
            str(data / "database.dump"),
        ],
        env=pg_env(restored),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    after = counts(restored)
    if after != manifest["table_counts"]:
        raise ValueError("Restore table counts differ from the authenticated backup")
    private = data / "private"
    for path in private.iterdir():
        path.rename(data / path.name)
    private.rmdir()
    archive.unlink()
    return {
        "database": database,
        "data_directory": str(data),
        "preserved_tables": len(after),
        "preserved_objects": sum(name.startswith("objects/") for name in manifest["files"]),
        "table_counts": after,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["backup", "rehearse"])
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    begin = time.perf_counter()
    old_mask = os.umask(0o077)
    try:
        key_path = LOCAL / "backup-encryption.key"
        if args.operation == "backup":
            path, manifest = backup(key_path)
            result = {
                "archive": str(path),
                "encrypted_bytes": path.stat().st_size,
                "tables": len(manifest["table_counts"]),
                "objects": sum(name.startswith("objects/") for name in manifest["files"]),
            }
        else:
            if args.archive is None:
                raise ValueError("An explicit encrypted archive is required")
            result = rehearse(args.archive, key_path)
        result["seconds"] = round(time.perf_counter() - begin, 3)
        result["scope"] = "actual native backup/restore; hosted/PITR/offsite/KMS NOT VERIFIED"
        print(json.dumps(result))
    finally:
        os.umask(old_mask)


if __name__ == "__main__":
    main()
