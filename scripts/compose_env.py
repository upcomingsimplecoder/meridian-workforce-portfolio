"""Create private, distinct local Compose credentials without printing them."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".local/compose.env"
target.parent.mkdir(mode=0o700, exist_ok=True)
if target.is_symlink():
    raise SystemExit("Compose credential file cannot be a symlink")
if not target.exists():
    values = {
        key: secrets.token_urlsafe(32)
        for key in (
            "MERIDIAN_DB_PASSWORD",
            "MERIDIAN_API_DB_PASSWORD",
            "MERIDIAN_WORKER_DB_PASSWORD",
            "MERIDIAN_IDENTITY_DB_PASSWORD",
        )
    }
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
elif target.stat().st_mode & 0o077:
    raise SystemExit("Compose credentials must be mode 0600")
print("Private Compose credentials ready in .local/compose.env (0600).")
