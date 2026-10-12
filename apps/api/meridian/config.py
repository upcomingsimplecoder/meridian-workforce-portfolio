from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def database_project(url: str) -> str | None:
    parsed = make_url(url)
    host = parsed.host or ""
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return host.split(".")[1]
    username = parsed.username or ""
    if host.endswith(".pooler.supabase.com") and "." in username:
        return username.split(".", 1)[1]
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: str = "local"
    runtime_kind: Literal["api", "worker"] = "api"
    database_url: str
    worker_database_url: str = ""
    oidc_issuer: str = "http://127.0.0.1:4017"
    oidc_internal_origin: str = ""
    oidc_client_id: str = "meridian-web"
    oidc_redirect_uri: str = "http://127.0.0.1:3017/auth/callback"
    web_origin: str = "http://127.0.0.1:3017"
    api_origin: str = "http://127.0.0.1:8017"
    cookie_secure: bool = False
    ai_enabled: bool = False
    local_data_dir: str = ".local"
    analytics_dbt_timeout_seconds: int = Field(default=600, ge=300, le=900)
    storage_backend: Literal["local", "supabase"] = "local"
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_storage_bucket: str = "meridian-artifacts"
    object_signing_key: SecretStr = SecretStr("")
    storage_timeout_seconds: float = Field(default=15.0, ge=1, le=30)
    otel_exporter_otlp_endpoint: str = ""

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if self.ai_enabled:
            raise ValueError("AI is not implemented and cannot be enabled")
        if self.app_env not in {"local", "test"}:
            if not self.cookie_secure or not all(
                url.startswith("https://")
                for url in (
                    self.oidc_issuer,
                    self.web_origin,
                    self.oidc_redirect_uri,
                    self.api_origin,
                )
            ):
                raise ValueError("Hosted operation requires HTTPS and secure cookies")
            origins = [
                urlsplit(url) for url in (self.oidc_issuer, self.web_origin, self.api_origin)
            ]
            if any(
                not url.hostname
                or url.hostname in {"localhost", "127.0.0.1", "::1"}
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                for url in origins
            ):
                raise ValueError("Hosted service origins must be exact HTTPS origins")
            if self.oidc_internal_origin not in {"", self.oidc_issuer}:
                raise ValueError("Hosted identity transport must use the exact HTTPS issuer")
            redirect = urlsplit(self.oidc_redirect_uri)
            if (
                redirect.netloc != origins[1].netloc
                or redirect.path != "/auth/callback"
                or redirect.username
                or redirect.password
                or redirect.query
                or redirect.fragment
            ):
                raise ValueError("Hosted callback must match the exact frontend origin")
            try:
                database = make_url(self.database_url)
            except Exception:
                raise ValueError("Invalid hosted database configuration") from None
            expected = "meridian_app" if self.runtime_kind == "api" else "meridian_worker"
            if (
                database.drivername != "postgresql+psycopg"
                or (database.username or "").split(".")[0] != expected
                or database.query.get("sslmode") != "verify-full"
                or not database.password
                or not database.host
            ):
                raise ValueError("Hosted runtime requires its dedicated TLS database credential")
            if self.storage_backend != "supabase":
                raise ValueError("Hosted operation requires the Supabase private object store")
            parsed = urlsplit(self.supabase_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or not self.supabase_service_role_key.get_secret_value()
            ):
                raise ValueError("Hosted operation requires Supabase storage credentials")
            project = database_project(self.database_url)
            if not project or parsed.hostname != project + ".supabase.co":
                raise ValueError("Hosted database and storage must belong to the same project")
            if not self.object_signing_key.get_secret_value():
                raise ValueError("Hosted operation requires a persistent object signing key")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
