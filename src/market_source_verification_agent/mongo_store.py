"""MongoDB-backed task store for production task metadata."""

from __future__ import annotations

import os
from datetime import datetime
from uuid import uuid4

from .config import Settings, load_settings
from .schema import Artifact, RunEvent, RunTask


class MongoTaskStore:
    """Synchronous PyMongo implementation of the task metadata store."""

    def __init__(self, settings: Settings | None = None, uri: str | None = None):
        self.settings = settings or load_settings()
        resolved_uri = uri or _mongo_uri(self.settings)
        if not resolved_uri:
            raise RuntimeError(f"Missing MongoDB URI env var: {self.settings.mongodb.uri_env}")
        try:
            from pymongo import ASCENDING, DESCENDING, MongoClient
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("MongoTaskStore requires pymongo") from exc

        self._ascending = ASCENDING
        self._descending = DESCENDING
        self.client = MongoClient(resolved_uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[self.settings.mongodb.database]
        self.runs = self.db[_collection(self.settings, "runs")]
        self.events = self.db[_collection(self.settings, "events")]
        self.artifacts = self.db[_collection(self.settings, "artifacts")]
        self.ensure_indexes()

    def ensure_indexes(self) -> None:
        self.runs.create_index([("run_id", self._ascending)], unique=True)
        self.runs.create_index([("owner_id", self._ascending), ("created_at", self._descending)])
        self.runs.create_index([("status", self._ascending), ("updated_at", self._ascending)])
        self.events.create_index([("run_id", self._ascending), ("created_at", self._ascending)])
        self.artifacts.create_index([("run_id", self._ascending)])
        self.artifacts.create_index([("sha256", self._ascending)])

    def create_run(
        self,
        owner_id: str,
        input_kind: str,
        requested_format: str,
        detailed: bool,
        input_filename: str | None = None,
        input_path: str | None = None,
    ) -> RunTask:
        now = datetime.now()
        task = RunTask(
            run_id=str(uuid4()),
            owner_id=owner_id,
            status="queued",
            input_kind=input_kind,  # type: ignore[arg-type]
            input_filename=input_filename,
            input_path=input_path,
            requested_format=requested_format,  # type: ignore[arg-type]
            detailed=detailed,
            created_at=now,
            updated_at=now,
        )
        self.save_task(task)
        self.add_event(task.run_id, "queued", "info", "Run queued")
        return task

    def get_task(self, run_id: str, owner_id: str) -> RunTask | None:
        doc = self.runs.find_one({"run_id": run_id, "owner_id": owner_id}, {"_id": False})
        return RunTask.model_validate(doc) if doc else None

    def save_task(self, task: RunTask) -> None:
        task.updated_at = datetime.now()
        self.runs.replace_one(
            {"run_id": task.run_id},
            task.model_dump(mode="python"),
            upsert=True,
        )

    def add_event(
        self,
        run_id: str,
        stage: str,
        level: str,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> RunEvent:
        event = RunEvent(
            event_id=str(uuid4()),
            run_id=run_id,
            stage=stage,
            level=level,  # type: ignore[arg-type]
            message=message,
            progress_current=progress_current,
            progress_total=progress_total,
            created_at=datetime.now(),
        )
        self.events.insert_one(event.model_dump(mode="python"))
        return event

    def list_events(self, run_id: str, owner_id: str) -> list[RunEvent]:
        task = self.get_task(run_id, owner_id)
        if not task:
            return []
        docs = self.events.find({"run_id": run_id}, {"_id": False}).sort("created_at", self._ascending)
        return [RunEvent.model_validate(doc) for doc in docs]

    def save_artifact(self, artifact: Artifact) -> None:
        self.artifacts.replace_one(
            {"artifact_id": artifact.artifact_id},
            artifact.model_dump(mode="python"),
            upsert=True,
        )

    def list_artifacts(self, run_id: str, owner_id: str) -> list[Artifact]:
        task = self.get_task(run_id, owner_id)
        if not task:
            return []
        docs = self.artifacts.find({"run_id": run_id, "owner_id": owner_id}, {"_id": False})
        return [Artifact.model_validate(doc) for doc in docs]

    def list_runs(self, owner_id: str, limit: int = 20, offset: int = 0) -> list[RunTask]:
        docs = (
            self.runs.find({"owner_id": owner_id}, {"_id": False})
            .sort("created_at", self._descending)
            .skip(offset)
            .limit(limit)
        )
        return [RunTask.model_validate(doc) for doc in docs]


def _collection(settings: Settings, name: str) -> str:
    return settings.mongodb.collections.get(name, name)


def _mongo_uri(settings: Settings) -> str | None:
    return os.getenv(settings.mongodb.uri_env) or os.getenv("MONGODB_URL")
