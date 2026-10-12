import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from meridian.compensation_seed import seed_compensation
from meridian.config import settings
from meridian.db import SessionLocal
from meridian.finance_seed import seed_finance
from meridian.models import DemoCredential, Principal, RoleAssignment
from meridian.object_store import ensure_key
from meridian.people_seed import seed_people
from meridian.talent_seed import seed_talent

PERSONAS = [
    ("hr", "HR Operations", "hr", "*"),
    ("finance", "Finance Partner", "finance", "*"),
    ("manager-a", "Manager · Research", "manager", "D01"),
    ("manager-b", "Manager · Operations", "manager", "D02"),
    ("approver", "Division Approver", "approver", "D01"),
    ("executive", "Executive Observer", "executive", "*"),
    ("auditor", "Governance Auditor", "auditor", "*"),
    ("steward", "Data Steward", "steward", "*"),
]


def seed() -> None:
    if settings().app_env not in {"local", "test"}:
        raise RuntimeError("Synthetic identity bootstrap is restricted to local/test environments")
    ensure_key()
    target = Path(settings().local_data_dir) / "demo-credentials.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials: dict[str, str] = json.loads(target.read_text()) if target.exists() else {}
    with SessionLocal.begin() as db:
        for name, label, role, scope in PERSONAS:
            if db.get(Principal, name) is None:
                db.add(Principal(id=name, oidc_sub=f"synthetic:{name}", display_name=label))
                db.flush()
                db.add(
                    RoleAssignment(
                        principal_id=name,
                        role=role,
                        scope=scope,
                        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                )
            if db.scalar(select(DemoCredential).where(DemoCredential.username == name)) is None:
                password = credentials.get(name) or secrets.token_urlsafe(18)
                credentials[name] = password
                salt = secrets.token_hex(16)
                hashed = hashlib.scrypt(
                    password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=64
                ).hex()
                db.add(
                    DemoCredential(
                        username=name, principal_id=name, salt=salt, password_hash=hashed
                    )
                )
        seed_people(db)
        seed_finance(db)
        seed_compensation(db)
        seed_talent(db)
    target.write_text(json.dumps(credentials, indent=2) + "\n")
    target.chmod(0o600)
    print("Seeded 8 synthetic identities. Credentials are in .local/demo-credentials.json (0600).")


if __name__ == "__main__":
    seed()
