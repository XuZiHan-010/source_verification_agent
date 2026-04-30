"""Backend selection helpers for API and worker processes."""

from __future__ import annotations

import os

from .config import Settings, load_settings
from .mongo_store import MongoTaskStore
from .tasks import LocalTaskStore, TaskStore


def create_task_store(settings: Settings | None = None) -> TaskStore:
    settings = settings or load_settings()
    backend = settings.runtime.task_store_backend
    if backend == "local":
        return LocalTaskStore(settings)
    if backend == "mongodb":
        return MongoTaskStore(settings)
    if os.getenv(settings.mongodb.uri_env) or os.getenv("MONGODB_URL"):
        try:
            return MongoTaskStore(settings)
        except Exception:
            return LocalTaskStore(settings)
    return LocalTaskStore(settings)
