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
from .usage import read_persistent_usage


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
        return FileResponse(ROOT / "web" / "demo.html", headers={"Cache-Control": "no-store"})

    @app.get("/web/demo.html")
    def demo_page_alias():
        return FileResponse(ROOT / "web" / "demo.html", headers={"Cache-Control": "no-store"})

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

    cleanup_interval_seconds = 3600  # 1 hour
    last_cleanup_time = {"value": 0}

    @app.get("/api/runs")
    def list_runs(
        owner_id: Annotated[str, Depends(current_owner)],
        limit: int = 20,
        offset: int = 0,
    ):
        """List runs and trigger periodic cleanup."""
        import time

        from .cleanup import run_cleanup

        # Trigger cleanup every hour in background
        now = time.time()
        if now - last_cleanup_time["value"] > cleanup_interval_seconds:
            last_cleanup_time["value"] = now
            try:
                run_cleanup(settings, dry_run=False)
            except Exception as exc:
                import logging

                logging.error(f"Background cleanup failed: {exc}")

        tasks = store.list_runs(owner_id, limit=limit, offset=offset)
        return [_run_payload(task, store, owner_id) for task in tasks]

    @app.get("/api/usage")
    def get_usage(owner_id: Annotated[str, Depends(current_owner)]):
        return read_persistent_usage(settings)

    @app.delete("/api/runs")
    def delete_runs(owner_id: Annotated[str, Depends(current_owner)]):
        deleted = store.delete_runs(owner_id)
        return {"deleted": deleted}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, owner_id: Annotated[str, Depends(current_owner)]):
        task = store.get_task(run_id, owner_id)
        if not task:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"run": _run_payload(task, store, owner_id), "events": store.list_events(run_id, owner_id)}

    @app.get("/api/runs/{run_id}/result")
    def get_result(run_id: str, owner_id: Annotated[str, Depends(current_owner)]):
        task = store.get_task(run_id, owner_id)
        if not task:
            raise HTTPException(status_code=404, detail="Run not found")
        if task.status != "completed":
            raise HTTPException(status_code=409, detail=f"Run is {task.status}")
        artifact = _select_artifact(store, run_id, owner_id, task.requested_format)
        path = _artifact_path_or_404(artifact)
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
        path = _artifact_path_or_404(artifact)
        return FileResponse(path, media_type=artifact.content_type, filename=artifact.filename)

    @app.get("/api/runs/{run_id}/input")
    def download_input(run_id: str, owner_id: Annotated[str, Depends(current_owner)]):
        task = store.get_task(run_id, owner_id)
        if not task:
            raise HTTPException(status_code=404, detail="Run not found")
        if not task.input_path:
            raise HTTPException(status_code=404, detail="Input file not available")
        path = path_from_storage_uri(task.input_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Input file not found on disk")
        import mimetypes
        media_type, _ = mimetypes.guess_type(str(path))
        filename = task.input_filename or path.name
        return FileResponse(path, media_type=media_type or "application/octet-stream", filename=filename)

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str, owner_id: Annotated[str, Depends(current_owner)]):
        if not store.delete_run(run_id, owner_id):
            raise HTTPException(status_code=404, detail="Run not found")
        return {"deleted": 1, "run_id": run_id}

    @app.get("/api/volume")
    def get_volume_usage():
        """Get current volume/disk usage (Railway monitoring)."""
        from pathlib import Path

        from .cleanup import get_dir_size

        usage = {
            "cache_mb": get_dir_size(Path(settings.storage.cache_dir if hasattr(settings.storage, "cache_dir") else "data/cache")) / 1024 / 1024,
            "uploads_mb": get_dir_size(Path(settings.storage.uploads_dir)) / 1024 / 1024,
            "reports_mb": get_dir_size(Path(settings.storage.reports_dir)) / 1024 / 1024,
        }
        usage["total_mb"] = sum(usage.values())
        usage["warning"] = usage["total_mb"] > 500
        return usage

    @app.post("/api/cleanup")
    def manual_cleanup():
        """Manually trigger cleanup of old runs and cache files."""
        from .cleanup import run_cleanup

        return run_cleanup(settings)

    return app


def _configured_api_keys(env_name: str) -> set[str]:
    raw = os.getenv(env_name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _run_payload(task, store, owner_id: str) -> dict:
    payload = task.model_dump(mode="json")
    payload["result_available"] = _has_artifact_file(store, task.run_id, owner_id, task.requested_format)
    payload["input_available"] = _path_exists(task.input_path)
    usage = (task.summary or {}).get("usage") if isinstance(task.summary, dict) else None
    payload["usage"] = usage or {
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "by_model": {},
    }
    payload["global_usage"] = read_persistent_usage(store.settings)
    payload["cost_usd"] = payload["usage"].get("estimated_cost_usd", 0.0)
    return payload


def _select_artifact(store, run_id: str, owner_id: str, fmt: str):
    artifacts = store.list_artifacts(run_id, owner_id)
    for artifact in artifacts:
        if artifact.fmt == fmt:
            return artifact
    from fastapi import HTTPException

    raise HTTPException(status_code=404, detail="Artifact not found")


def _has_artifact_file(store, run_id: str, owner_id: str, fmt: str) -> bool:
    try:
        artifact = _select_artifact(store, run_id, owner_id, fmt)
    except Exception:
        return False
    return _path_exists(artifact.storage_uri)


def _path_exists(storage_uri: str | None) -> bool:
    if not storage_uri:
        return False
    try:
        return path_from_storage_uri(storage_uri).exists()
    except Exception:
        return False


def _artifact_path_or_404(artifact):
    from fastapi import HTTPException

    path = path_from_storage_uri(artifact.storage_uri)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Result artifact not found on disk")
    return path


def _read_json(path: Path):
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    return _flatten_sectioned_result(payload)


def _flatten_sectioned_result(payload):
    if not isinstance(payload, list) or not any(isinstance(item, dict) and isinstance(item.get("rows"), list) for item in payload):
        return payload

    rows = []
    for section_idx, section in enumerate(payload, start=1):
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or f"section {section_idx}")
        for row_idx, row in enumerate(section.get("rows") or [], start=1):
            if not isinstance(row, dict):
                continue
            rows.append(_section_row_to_legacy_row(row, title, section_idx, row_idx))
    return rows


def _section_row_to_legacy_row(row: dict, section_title: str, section_idx: int, row_idx: int) -> dict:
    claim_id = _first_matching_value(row, ("claimid", "id", "编号", "序号")) or f"S{section_idx}R{row_idx}"
    source_text = _first_matching_value(row, ("来源url", "来源", "来源名称", "source"))
    detail_text = str(row.get("多源核验明细") or "")
    urls = _dedupe(_extract_urls(source_text) + _extract_urls(detail_text))
    source_name = _strip_urls(source_text).strip() or (urls[0] if urls else "")
    skipped = {"来源是否真实", "原因", "核验诊断", "多源核验明细", "来源类别", "核验佐证"}
    statement_parts = []
    original_parts = []

    for key, value in row.items():
        text = str(value or "").strip()
        if not text or key in skipped:
            continue
        original_parts.append(f"{key}: {text}")
        normalized = _normalize_header(key)
        if normalized in {"claimid", "id", "编号", "序号"} or "来源" in normalized or "source" in normalized:
            continue
        statement_parts.append(f"{key}: {text}")

    statement = "；".join(statement_parts) or "；".join(original_parts) or claim_id
    diagnosis = row.get("核验诊断") or row.get("原因") or row.get("多源核验明细") or ""
    out = {
        "章节": section_title,
        "指标": section_title,
        "数值": "",
        "年份": _first_matching_value(row, ("年份", "时间", "日期", "year")),
        "地区/口径": _first_matching_value(row, ("地区", "口径", "区域", "region")),
        "事实声明": statement,
        "来源名称": source_name,
        "来源URL提示": urls[0] if urls else "",
        "来源URL列表": "\n".join(urls),
        "重复条数": "1",
        "原始Claim列表": claim_id,
        "核验诊断": diagnosis,
        "多源核验明细": detail_text,
        "来源是否真实": row.get("来源是否真实") or "❓ 无法验证",
        "来源类别": row.get("来源类别") or "C",
        "原始列": "\n".join(original_parts),
    }
    if row.get("核验佐证"):
        out["核验佐证"] = row.get("核验佐证")
    return out


def _normalize_header(value: str) -> str:
    import re

    return re.sub(r"\s+", "", str(value or "")).lower()


def _first_matching_value(row: dict, needles: tuple[str, ...]) -> str:
    normalized_needles = tuple(_normalize_header(item) for item in needles)
    for key, value in row.items():
        normalized = _normalize_header(key)
        if any(needle == normalized or needle in normalized for needle in normalized_needles):
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _extract_urls(value: str) -> list[str]:
    import re

    return [match.rstrip(").,;，。；") for match in re.findall(r"https?://[^\s<>\"')，。；;]+", str(value or ""))]


def _strip_urls(value: str) -> str:
    import re

    return re.sub(r"https?://[^\s<>\"')，。；;]+", "", str(value or ""))


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


app = create_app()
