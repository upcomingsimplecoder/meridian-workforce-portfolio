import os
from collections.abc import Generator
from urllib.parse import urlsplit

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from meridian.config import database_project, settings


class Base(DeclarativeBase):
    pass


def database_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"options": "-c statement_timeout=15000 -c timezone=UTC"},
    )


engine = database_engine(settings().database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def worker_session_factory() -> sessionmaker[Session]:
    # A worker must never inherit the API connection by accident. Hosted
    # services set WORKER_DATABASE_URL to the separately provisioned account;
    # tests and local jobs can use the value held in Settings, but a generic
    # DATABASE_URL is deliberately ignored here.
    cfg = settings()
    url = os.environ.get("WORKER_DATABASE_URL") or cfg.worker_database_url
    if not url:
        raise RuntimeError("WORKER_DATABASE_URL is required for a standalone worker")
    if getattr(cfg, "app_env", "local") not in {"local", "test"}:
        try:
            parsed = make_url(url)
        except Exception:
            raise RuntimeError("Invalid worker database configuration") from None
        if (
            parsed.drivername != "postgresql+psycopg"
            or (parsed.username or "").split(".")[0] != "meridian_worker"
            or parsed.query.get("sslmode") != "verify-full"
            or not parsed.password
            or not parsed.host
        ):
            raise RuntimeError("Hosted worker requires its dedicated TLS database credential")
        project = database_project(url)
        if not project or urlsplit(cfg.supabase_url).hostname != project + ".supabase.co":
            raise RuntimeError("Hosted worker database and storage must belong to the same project")
    return sessionmaker(database_engine(url), expire_on_commit=False)


def get_db() -> Generator[Session]:
    with SessionLocal() as db:
        yield db
