"""Local task service used by the FastAPI layer.

The production design in docs/07_orchestrator.md targets MongoDB + Redis/RQ.
This module keeps the same RunTask/Event/Artifact contracts while using the
filesystem as a development backend, so the API is usable before stage-three
infrastructure is provisioned.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .config import ROOT, Settings, load_settings
from .orchestrator import run
from .schema import Artifact, RunEvent, RunTask

CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


class TaskStore(Protocol):
    settings: Settings

    def create_run(
        self,
        owner_id: str,
        input_kind: str,
        requested_format: str,
        detailed: bool,
        input_filename: str | None = None,
        input_path: str | None = None,
    ) -> RunTask: ...

    def get_task(self, run_id: str, owner_id: str) -> RunTask | None: ...

    def save_task(self, task: RunTask) -> None: ...

    def add_event(
        self,
        run_id: str,
        stage: str,
        level: str,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> RunEvent: ...

    def list_events(self, run_id: str, owner_id: str) -> list[RunEvent]: ...

    def save_artifact(self, artifact: Artifact) -> None: ...

    def list_artifacts(self, run_id: str, owner_id: str) -> list[Artifact]: ...

    def list_runs(self, owner_id: str, limit: int = 20, offset: int = 0) -> list[RunTask]: ...


class LocalTaskStore:
    """Tiny JSON-backed task store for local development and tests."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.root = _abs_path(self.settings.storage.reports_dir).parent / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

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
        path = self._task_path(run_id)
        if not path.exists():
            return None
        task = RunTask.model_validate_json(path.read_text(encoding="utf-8"))
        return task if task.owner_id == owner_id else None

    def save_task(self, task: RunTask) -> None:
        task.updated_at = datetime.now()
        run_dir = self._run_dir(task.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._task_path(task.run_id).write_text(task.model_dump_json(indent=2), encoding="utf-8")

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
        path = self._events_path(run_id)
        events = []
        if path.exists():
            events = json.loads(path.read_text(encoding="utf-8"))
        events.append(event.model_dump(mode="json"))
        path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        return event

    def list_events(self, run_id: str, owner_id: str) -> list[RunEvent]:
        if not self.get_task(run_id, owner_id):
            return []
        path = self._events_path(run_id)
        if not path.exists():
            return []
        return [RunEvent.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def save_artifact(self, artifact: Artifact) -> None:
        path = self._artifacts_path(artifact.run_id)
        artifacts = []
        if path.exists():
            artifacts = json.loads(path.read_text(encoding="utf-8"))
        artifacts = [item for item in artifacts if item.get("artifact_id") != artifact.artifact_id]
        artifacts.append(artifact.model_dump(mode="json"))
        path.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_artifacts(self, run_id: str, owner_id: str) -> list[Artifact]:
        if not self.get_task(run_id, owner_id):
            return []
        path = self._artifacts_path(run_id)
        if not path.exists():
            return []
        return [Artifact.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def list_runs(self, owner_id: str, limit: int = 20, offset: int = 0) -> list[RunTask]:
        tasks = []
        for run_dir in self.root.iterdir():
            if not run_dir.is_dir():
                continue
            task_path = run_dir / "run.json"
            if not task_path.exists():
                continue
            try:
                task = RunTask.model_validate_json(task_path.read_text(encoding="utf-8"))
                if task.owner_id == owner_id:
                    tasks.append(task)
            except Exception:
                continue
        tasks.sort(key=lambda t: t.created_at or datetime.min, reverse=True)
        return tasks[offset : offset + limit]

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _task_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _events_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events.json"

    def _artifacts_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "artifacts.json"


def create_text_input(run_id: str, text: str, settings: Settings) -> Path:
    upload_dir = _abs_path(settings.storage.uploads_dir) / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / "input.md"
    path.write_text(text, encoding="utf-8")
    return path


def create_file_input(run_id: str, filename: str, content: bytes, settings: Settings) -> Path:
    upload_dir = _abs_path(settings.storage.uploads_dir) / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name or "input"
    path = upload_dir / safe_name
    path.write_bytes(content)
    return path


def execute_run(run_id: str, owner_id: str, store: TaskStore | None = None) -> RunTask:
    store = store or LocalTaskStore()
    task = store.get_task(run_id, owner_id)
    if task is None:
        raise KeyError(f"run not found: {run_id}")

    try:
        task.status = "running"
        task.current_stage = "ingest"
        task.started_at = datetime.now()
        store.save_task(task)
        store.add_event(run_id, "running", "info", "Run started")

        output_dir = _abs_path(store.settings.storage.reports_dir) / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"result.{task.requested_format}"
        report = run(
            task.input_path or "",
            out_path=output_path,
            fmt=task.requested_format,
            config=store.settings,
            detailed=task.detailed,
        )

        artifact = artifact_from_file(output_path, task.run_id, task.owner_id, task.requested_format)
        store.save_artifact(artifact)

        task.status = "completed"
        task.current_stage = "completed"
        task.finished_at = datetime.now()
        task.summary = report.summary
        task.total_claims = report.summary.get("total", 0)
        task.completed_claims = task.total_claims
        task.artifact_ids = [artifact.artifact_id]
        store.save_task(task)
        store.add_event(run_id, "completed", "info", "Run completed", task.completed_claims, task.total_claims)
        return task
    except Exception as exc:
        task.status = "failed"
        task.current_stage = "failed"
        task.error = str(exc)
        task.finished_at = datetime.now()
        store.save_task(task)
        store.add_event(run_id, "failed", "error", str(exc))
        return task


def artifact_from_file(path: Path, run_id: str, owner_id: str, fmt: str) -> Artifact:
    body = path.read_bytes()
    content_type = CONTENT_TYPES.get(fmt) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return Artifact(
        artifact_id=str(uuid4()),
        run_id=run_id,
        owner_id=owner_id,
        fmt=fmt,  # type: ignore[arg-type]
        storage_uri=path.resolve().as_uri(),
        filename=path.name,
        content_type=content_type,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        created_at=datetime.now(),
    )


def path_from_storage_uri(storage_uri: str) -> Path:
    if storage_uri.startswith("file:///"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(storage_uri)
        path = unquote(parsed.path)
        if parsed.netloc:
            return Path(f"//{parsed.netloc}{path}")
        if len(path) >= 4 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return Path(path)
    return Path(storage_uri)


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _abs_path(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate
