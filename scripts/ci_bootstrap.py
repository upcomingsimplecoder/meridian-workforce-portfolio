"""Prepare an ephemeral PostgreSQL service supplied by GitHub Actions."""

import os
import subprocess
from pathlib import Path

import psycopg

password = os.environ["MERIDIAN_CI_DB_PASSWORD"]
url = f"postgresql://meridian:{password}@127.0.0.1:54329"
with psycopg.connect(url + "/postgres", autocommit=True) as conn:
    for name in ("meridian_demo", "meridian_test"):
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
data = Path(".local").resolve()
data.mkdir(mode=0o700, exist_ok=True)
admin_path = data / "admin-database-url"
admin_path.write_text(url.replace("postgresql://", "postgresql+psycopg://") + "/meridian_demo\n")
admin_path.chmod(0o600)
values = {
    "APP_ENV": "test",
    "DATABASE_URL": url.replace("postgresql://", "postgresql+psycopg://") + "/meridian_demo",
    "OIDC_DATABASE_URL": url + "/meridian_demo",
    "OIDC_ISSUER": "http://127.0.0.1:4017",
    "OIDC_CLIENT_ID": "meridian-web",
    "OIDC_REDIRECT_URI": "http://127.0.0.1:3017/auth/callback",
    "WEB_ORIGIN": "http://127.0.0.1:3017",
    "COOKIE_SECURE": "false",
    "LOCAL_DATA_DIR": str(data),
}
Path(".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
Path(".env").chmod(0o600)
subprocess.run(["node", "scripts/identity-keys.mjs"], check=True)
