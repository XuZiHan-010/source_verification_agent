"""FastAPI application for task-style source verification."""

import os
from pathlib import Path
from typing import Annotated

from .backends import create_task_store
from .config import ROOT
from .config import load_settings
from .queueing import enqueue_run
from .tasks import (
    create_file_input,
    create_text_input,
    execute_run,
    owner_hash,
    path_from_storage_uri,
)


def create_app():
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    try:
        from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse
    except ImportError as exc:  # pragma: no cover - import-time environment guard
        raise RuntimeError("FastAPI API requires fastapi and python-multipart dependencies") from exc

    settings = load_settings()
    store = create_task_store(settings)
    app = FastAPI(title="Market Source Verification Agent", version="0.1.0")

    cors_origins = settings.web.effective_cors_origins()
    if cors_origins:
        allow_credentials = "*" not in cors_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def current_owner(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> str:
        configured = _configured_api_keys(settings.auth.api_keys_env)
        if configured:
            if not x_api_key or x_api_key not in configured:
                raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
            return owner_hash(x_api_key)
        if settings.auth.require_auth and x_api_key:
            return owner_hash(x_api_key)
        return owner_hash("anonymous-local-dev")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def demo_page():
        return FileResponse(ROOT / "web" / "demo.html")

    @app.get("/web/demo.html")
    def demo_page_alias():
        return FileResponse(ROOT / "web" / "demo.html")

    @app.post("/api/runs")
    async def create_run_endpoint(
        background_tasks: BackgroundTasks,
        owner_id: Annotated[str, Depends(current_owner)],
        file: UploadFile | None = File(default=None),
        text: str | None = Form(default=None),
        fmt: str = Form(default=None),
        detailed: bool = Form(default=False),
    ):
        requested_format = fmt or settings.output.default_format
        if requested_format not in {"xlsx", "md", "html", "json"}:
            raise HTTPException(status_code=400, detail="fmt must be one of xlsx, md, html, json")
        if file is None and not text:
            raise HTTPException(status_code=400, detail="Provide either file or text")

        task = store.create_run(
            owner_id=owner_id,
            input_kind="file" if file else "text",
            requested_format=requested_format,
            detailed=detailed,
            input_filename=file.filename if file else None,
        )

        if file:
            content = await file.read()
            input_path = create_file_input(task.run_id, file.filename or "input", content, settings)
        else:
            input_path = create_text_input(task.run_id, text or "", settings)

        task.input_path = str(input_path)
        store.save_task(task)
        if not enqueue_run(task.run_id, owner_id, settings):
            background_tasks.add_task(execute_run, task.run_id, owner_id, store)
        return task

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, owner_id: Annotated[str, Depends(current_owner)]):
        task = store.get_task(run_id, owner_id)
        if not task:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"run": task, "events": store.list_events(run_id, owner_id)}

    @app.get("/api/runs/{run_id}/result")
    def get_result(run_id: str, owner_id: Annotated[str, Depends(current_owner)]):
        task = store.get_task(run_id, owner_id)
        if not task:
            raise HTTPException(status_code=404, detail="Run not found")
        if task.status != "completed":
            raise HTTPException(status_code=409, detail=f"Run is {task.status}")
        artifact = _select_artifact(store, run_id, owner_id, task.requested_format)
        path = path_from_storage_uri(artifact.storage_uri)
        if artifact.fmt == "json":
            return _read_json(path)
        return {"artifact": artifact, "text": path.read_text(encoding="utf-8", errors="replace")}

    @app.get("/api/runs/{run_id}/download")
    def download(run_id: str, owner_id: Annotated[str, Depends(current_owner)], fmt: str | None = None):
        task = store.get_task(run_id, owner_id)
        if not task:
            raise HTTPException(status_code=404, detail="Run not found")
        if task.status != "completed":
            raise HTTPException(status_code=409, detail=f"Run is {task.status}")
        artifact = _select_artifact(store, run_id, owner_id, fmt or task.requested_format)
        path = path_from_storage_uri(artifact.storage_uri)
        return FileResponse(path, media_type=artifact.content_type, filename=artifact.filename)

    return app


def _configured_api_keys(env_name: str) -> set[str]:
    raw = os.getenv(env_name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _select_artifact(store, run_id: str, owner_id: str, fmt: str):
    artifacts = store.list_artifacts(run_id, owner_id)
    for artifact in artifacts:
        if artifact.fmt == fmt:
            return artifact
    from fastapi import HTTPException

    raise HTTPException(status_code=404, detail="Artifact not found")


def _read_json(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


app = create_app()
