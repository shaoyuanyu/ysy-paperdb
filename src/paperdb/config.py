from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, all PAPERDB_* plus metadata-source aliases."""

    model_config = SettingsConfigDict(
        env_prefix="PAPERDB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://paperdb:paperdb_dev_password@127.0.0.1:5432/paperdb"
    service_token: str = "pdb_svc_dev0123456789abcdef0123456789abcdef"
    token_pepper: str = "dev-pepper-change-me-0123456789abcdef"

    storage_backend: Literal["local", "s3", "oss"] = "local"
    storage_root: Path = Path("./data/files")
    max_upload_mb: int = 200

    cors_origins: str = ""
    public_url: str = "http://127.0.0.1:9720"

    # Metadata-source credentials. "me" is an explicit placeholder until a real
    # contact address is configured; ingesters must send it as mailto/User-Agent.
    openalex_mailto: str = Field(default="me", validation_alias="OPENALEX_MAILTO")
    crossref_mailto: str = Field(default="me", validation_alias="CROSSREF_MAILTO")
    arxiv_user_agent: str = Field(
        default="paperdb/0.1 (mailto:me)", validation_alias="ARXIV_USER_AGENT"
    )
    s2_api_key: str | None = Field(default=None, validation_alias="S2_API_KEY")
    # Same secret as the site login (TOTP, RFC 6238 SHA1/6-digit/30s). When
    # set, personal tokens must present a current code for destructive
    # endpoints; the service token bypasses the gate.
    totp_secret: str | None = Field(default=None, validation_alias="PAPERDB_TOTP_SECRET")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
