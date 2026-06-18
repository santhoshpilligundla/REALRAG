from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    pg_data_dir: Path = Field(default=Path("./storage/pg-data"))
    pg_db_name: str = "realrag"

    anthropic_api_key: str = ""
    openai_api_key: str = ""

    embedding_model: str = "openai:text-embedding-3-large"
    default_chat_provider: str = "anthropic"
    default_anthropic_model: str = "claude-haiku-4-5"
    mid_anthropic_model: str = "claude-sonnet-4-6"
    reasoning_anthropic_model: str = "claude-opus-4-7"

    log_level: str = "INFO"

    # Extra knowledge dirs (colon-separated paths)
    # EXTRA_DOC_DIRS  = markdown/HTML docs to embed into the business index
    # EXTRA_SCHEMA_DIRS = folders of .sql DDL files to embed as schema knowledge
    extra_doc_dirs: str = ""
    extra_schema_dirs: str = ""

    def get_extra_doc_dirs(self) -> list:
        return [p for p in self.extra_doc_dirs.split(";") if p.strip()]

    def get_extra_schema_dirs(self) -> list:
        return [p for p in self.extra_schema_dirs.split(";") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
