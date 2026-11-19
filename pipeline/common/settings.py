"""Runtime settings. Secrets are read from files (Docker secrets), never from plain env vars."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(path: str | None) -> SecretStr | None:
    if not path:
        return None
    return SecretStr(Path(path).read_text(encoding="utf-8").strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAGEBOX_", extra="ignore")

    profile: Literal["cloud", "gpu", "test"] = "cloud"
    database_url: str = "sqlite+pysqlite:///:memory:"  # password injected from file below
    database_password_file: str | None = None

    s3_endpoint: str = "http://objects:8333"
    s3_access_key_file: str | None = None
    s3_secret_key_file: str | None = None

    session_secret_file: str | None = None
    media_signing_key_file: str | None = None
    together_api_key_file: str | None = None

    media_url_ttl_seconds: int = 300
    session_ttl_hours: int = 12
    max_upload_bytes: int = 200 * 1024 * 1024
    imports_dir: str = "/imports"  # read-only mount of server-side sources (bare file names)
    prefect_api_url: str = "http://prefect-server:4200/api"  # internal network only
    cookie_secure: bool = True

    @model_validator(mode="after")
    def _ttl_bounds(self) -> Settings:
        if not 1 <= self.media_url_ttl_seconds <= 300:
            raise ValueError("signed media URLs must expire within 5 minutes")
        return self

    def secret(
        self,
        name: Literal[
            "database_password",
            "s3_access_key",
            "s3_secret_key",
            "session_secret",
            "media_signing_key",
            "together_api_key",
        ],
    ) -> SecretStr | None:
        value: str | None = getattr(self, f"{name}_file")
        return _read_secret(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
