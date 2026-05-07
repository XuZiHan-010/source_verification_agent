"""Configuration loading helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field


# In production (installed via pip), override with PROJECT_ROOT env var to point to /app
ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[2]))
_DOTENV_LOADED = False


def load_environment() -> None:
    """Load local .env values without overriding real deployment env vars."""

    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        pass


class ModelSettings(BaseModel):
    extractor: str = "gpt-4o-mini"
    verifier: str = "gpt-4o-mini"
    classifier: str = "gpt-4o-mini"


class ConcurrencySettings(BaseModel):
    fetch_workers: int = 8
    llm_workers: int = 4
    llm_global_max: int = 8
    llm_global_backend: str = "redis"
    per_domain_max: int = 2


class CacheSettings(BaseModel):
    dir: str = "cache/"
    fetch_ttl_days: int = 30
    verify_ttl_days: int = 7
    classify_ttl_days: int = 90


class OutputSettings(BaseModel):
    default_format: str = "xlsx"
    include_detail_column: bool = False


class LimitSettings(BaseModel):
    max_claims_per_run: int = 2000
    per_claim_max_tokens: int = 8000
    llm_extract_max_calls_per_run: int = 50


class StorageSettings(BaseModel):
    backend: str = "local"
    uploads_dir: str = "data/uploads"
    reports_dir: str = "data/reports"


class SearchSettings(BaseModel):
    provider: str = "none"
    api_key_env: str | None = None


class MongoSettings(BaseModel):
    uri_env: str = "MONGODB_URI"
    database: str = "market_source_verification"
    collections: dict[str, str] = Field(default_factory=dict)


class QueueSettings(BaseModel):
    backend: str = "auto"
    redis_url_env: str = "REDIS_URL"
    default_queue: str = "source-verification"
    worker_concurrency: int = 2
    job_timeout: int = 1800
    result_ttl: int = 86400
    failure_ttl: int = 604800
    zombie_scan_interval: int = 300


class AuthSettings(BaseModel):
    api_keys_env: str = "API_KEYS"
    session_token_ttl: int = 2592000
    require_auth: bool = True


class WebSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=list)

    def effective_cors_origins(self) -> list[str]:
        env_val = os.environ.get("CORS_ORIGINS", "").strip()
        if env_val:
            return [o.strip() for o in env_val.split(",") if o.strip()]
        return self.cors_origins


class RuntimeSettings(BaseModel):
    task_store_backend: str = "auto"


class Settings(BaseModel):
    models: ModelSettings = Field(default_factory=ModelSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    mongodb: MongoSettings = Field(default_factory=MongoSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    usage_callback: Callable[[dict[str, Any]], None] | None = Field(default=None, exclude=True)

    model_config = {"extra": "ignore", "arbitrary_types_allowed": True}


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(path: str | Path | None = None) -> Settings:
    load_environment()
    cfg_path = Path(path or os.getenv("MARKET_SOURCE_SETTINGS") or ROOT / "config" / "settings.yaml")
    if not cfg_path.exists():
        return Settings()
    return Settings.model_validate(load_yaml(cfg_path))


def load_source_tiers(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else ROOT / "config" / "source_tiers.yaml"
    return load_yaml(cfg_path) if cfg_path.exists() else {"tiers": {}}
