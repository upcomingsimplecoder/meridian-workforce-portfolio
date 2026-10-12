"""Bounded streaming AES-256-GCM; authenticated before any restore command runs."""

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"MERIDIAN-AESGCM-1"
CHUNK = 1024 * 1024
MAX_ARCHIVE = 2 * 1024 * 1024 * 1024


def private_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Backup key must be a regular private mode-0600 file")
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError("AES-256 backup key must contain exactly 32 bytes")
    return value


def new_private(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def validate_source(path: Path, *, encrypted: bool = False) -> int:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Backup input must be a regular private file")
    size = path.stat().st_size
    if size > MAX_ARCHIVE + (len(MAGIC) + 28 if encrypted else 0):
        raise ValueError("Backup exceeds its bounded archive limit")
    return size


def encrypt_file(source: Path, target: Path, key: bytes) -> None:
    validate_source(source)
    if len(key) != 32:
        raise ValueError("AES-256 backup key must contain exactly 32 bytes")
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    created = False
    try:
        with source.open("rb") as plain, new_private(target) as sealed:
            created = True
            sealed.write(MAGIC + nonce)
            while chunk := plain.read(CHUNK):
                sealed.write(encryptor.update(chunk))
            sealed.write(encryptor.finalize())
            sealed.write(encryptor.tag)
            sealed.flush()
            os.fsync(sealed.fileno())
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def decrypt_file(source: Path, target: Path, key: bytes) -> None:
    size = validate_source(source, encrypted=True)
    if len(key) != 32 or size < len(MAGIC) + 28:
        raise ValueError("Invalid encrypted backup structure or key length")
    created = False
    try:
        with source.open("rb") as sealed:
            if sealed.read(len(MAGIC)) != MAGIC:
                raise ValueError("Unsupported encrypted backup format")
            nonce = sealed.read(12)
            remaining = size - len(MAGIC) - 28
            sealed.seek(-16, os.SEEK_END)
            tag = sealed.read(16)
            sealed.seek(len(MAGIC) + 12)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            with new_private(target) as plain:
                created = True
                while remaining:
                    chunk = sealed.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ValueError("Truncated encrypted backup")
                    remaining -= len(chunk)
                    plain.write(decryptor.update(chunk))
                # Caller may invoke pg_restore only after this succeeds. Failed
                # authentication removes every byte of the private scratch file.
                plain.write(decryptor.finalize())
                plain.flush()
                os.fsync(plain.fileno())
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
