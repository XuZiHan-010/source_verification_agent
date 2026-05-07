from pathlib import Path
import json
import shutil
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from market_source_verification_agent.cleanup import cleanup_old_runs, enforce_cache_size_limit
from market_source_verification_agent.config import Settings
from market_source_verification_agent.schema import RunTask
from market_source_verification_agent.tasks import LocalTaskStore, _write_xlsx_from_json_report, owner_hash


def test_fastapi_text_run_smoke(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    monkeypatch.setenv("API_KEYS", "")
    test_root = Path.cwd() / "data" / "test_api" / uuid4().hex
    test_root.mkdir(parents=True, exist_ok=True)
    settings_path = test_root / "settings.yaml"
    settings_path.write_text(
        f"""
storage:
  uploads_dir: {test_root / "uploads"}
  reports_dir: {test_root / "reports"}
cache:
  dir: {test_root / "cache"}
output:
  default_format: json
auth:
  require_auth: false
queue:
  backend: local
runtime:
  task_store_backend: local
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKET_SOURCE_SETTINGS", str(settings_path))

    from fastapi.testclient import TestClient

    from market_source_verification_agent.api import create_app

    # create_app reads the repository settings by default; patch the loader path
    import market_source_verification_agent.api as api_module

    original_load_settings = api_module.load_settings
    api_module.load_settings = lambda: original_load_settings(settings_path)
    try:
        client = TestClient(create_app())
    finally:
        api_module.load_settings = original_load_settings

    text = """
# Demo

| 指标 | 数值 | 年份 | 来源名称 |
|---|---|---|---|
| 低空经济规模 | 5059.5 亿元 | 2023 | 赛迪研究院/赛迪智库 |
"""
    response = client.post("/api/runs", data={"text": text, "fmt": "json"})
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    status = client.get(f"/api/runs/{run_id}")
    assert status.status_code == 200
    assert status.json()["run"]["status"] in {"completed", "failed"}

    # A source without a URL is allowed to fail verification; the API contract
    # should still persist a terminal task instead of hanging in queued/running.
    assert status.json()["run"]["current_stage"] in {"completed", "failed"}

    delete_response = client.delete(f"/api/runs/{run_id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] == 1

    missing = client.get(f"/api/runs/{run_id}")
    assert missing.status_code == 404


def test_local_task_store_ignores_empty_run_json():
    test_root = Path.cwd() / "data" / "test_task_store" / uuid4().hex
    settings = Settings(storage={"uploads_dir": str(test_root / "uploads"), "reports_dir": str(test_root / "reports")})
    try:
        store = LocalTaskStore(settings)
        owner_id = owner_hash("anonymous-local-dev")
        run_id = "empty-run"
        run_dir = store.root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("", encoding="utf-8")

        assert store.get_task(run_id, owner_id) is None
        assert store.list_events(run_id, owner_id) == []
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_local_task_store_writes_parseable_task_json():
    test_root = Path.cwd() / "data" / "test_task_store" / uuid4().hex
    settings = Settings(storage={"uploads_dir": str(test_root / "uploads"), "reports_dir": str(test_root / "reports")})
    try:
        store = LocalTaskStore(settings)
        owner_id = owner_hash("anonymous-local-dev")

        task = store.create_run(owner_id, "text", "json", False)
        task.status = "running"
        store.save_task(task)

        loaded = store.get_task(task.run_id, owner_id)
        assert loaded is not None
        assert loaded.status == "running"
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_json_report_can_be_exported_to_xlsx():
    pytest.importorskip("openpyxl")
    test_root = Path.cwd() / "data" / "test_task_store" / uuid4().hex
    source_path = test_root / "result.json"
    target_path = test_root / "result.xlsx"
    try:
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(
            json.dumps(
                [
                    {
                        "title": "核验报告",
                        "headers": ["Claim ID", "指标", "来源类别"],
                        "rows": [{"Claim ID": "T11", "指标": "技术名称", "来源类别": "C"}],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        _write_xlsx_from_json_report(source_path, target_path)

        assert target_path.exists()
        assert target_path.stat().st_size > 0
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_fastapi_delete_all_runs(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    monkeypatch.setenv("API_KEYS", "")
    test_root = Path.cwd() / "data" / "test_api" / uuid4().hex
    test_root.mkdir(parents=True, exist_ok=True)
    settings_path = test_root / "settings.yaml"
    settings_path.write_text(
        f"""
storage:
  uploads_dir: {test_root / "uploads"}
  reports_dir: {test_root / "reports"}
cache:
  dir: {test_root / "cache"}
output:
  default_format: json
auth:
  require_auth: false
queue:
  backend: local
runtime:
  task_store_backend: local
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKET_SOURCE_SETTINGS", str(settings_path))

    from fastapi.testclient import TestClient

    from market_source_verification_agent.api import create_app

    import market_source_verification_agent.api as api_module

    original_load_settings = api_module.load_settings
    api_module.load_settings = lambda: original_load_settings(settings_path)
    try:
        client = TestClient(create_app())
    finally:
        api_module.load_settings = original_load_settings

    for idx in range(2):
        response = client.post("/api/runs", data={"text": f"测试文本 {idx}", "fmt": "json"})
        assert response.status_code == 200

    assert len(client.get("/api/runs").json()) == 2

    delete_response = client.delete("/api/runs")
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] == 2
    assert client.get("/api/runs").json() == []


def test_fastapi_missing_history_artifact_is_404(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    monkeypatch.setenv("API_KEYS", "")
    test_root = Path.cwd() / "data" / "test_api" / uuid4().hex
    test_root.mkdir(parents=True, exist_ok=True)
    settings_path = test_root / "settings.yaml"
    settings_path.write_text(
        f"""
storage:
  uploads_dir: {test_root / "uploads"}
  reports_dir: {test_root / "reports"}
cache:
  dir: {test_root / "cache"}
output:
  default_format: json
auth:
  require_auth: false
queue:
  backend: local
runtime:
  task_store_backend: local
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKET_SOURCE_SETTINGS", str(settings_path))

    from fastapi.testclient import TestClient

    from market_source_verification_agent.api import create_app
    from market_source_verification_agent.tasks import LocalTaskStore, artifact_from_file, owner_hash
    from market_source_verification_agent.usage import add_persistent_usage

    import market_source_verification_agent.api as api_module

    original_load_settings = api_module.load_settings
    api_module.load_settings = lambda: original_load_settings(settings_path)
    try:
        settings = original_load_settings(settings_path)
        add_persistent_usage(
            settings,
            {
                "model": "gpt-4o-mini",
                "calls": 1,
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "output_tokens": 20,
                "total_tokens": 120,
                "estimated_cost_usd": 0.000027,
            },
        )
        store = LocalTaskStore(settings)
        owner_id = owner_hash("anonymous-local-dev")
        task = store.create_run(
            owner_id=owner_id,
            input_kind="text",
            requested_format="json",
            detailed=False,
        )
        task.status = "completed"
        task.current_stage = "completed"
        store.save_task(task)

        output_dir = Path(settings.storage.reports_dir) / task.run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        result_path = output_dir / "result.json"
        result_path.write_text("[]", encoding="utf-8")
        artifact = artifact_from_file(result_path, task.run_id, owner_id, "json")
        store.save_artifact(artifact)
        task.artifact_ids = [artifact.artifact_id]
        store.save_task(task)

        result_path.unlink()
        client = TestClient(create_app())
    finally:
        api_module.load_settings = original_load_settings

    runs_response = client.get("/api/runs")
    assert runs_response.status_code == 200
    assert runs_response.json()[0]["result_available"] is False

    status_response = client.get(f"/api/runs/{task.run_id}")
    assert status_response.status_code == 200
    assert status_response.json()["run"]["result_available"] is False

    result_response = client.get(f"/api/runs/{task.run_id}/result")
    assert result_response.status_code == 404
    assert result_response.json()["detail"] == "Result artifact not found on disk"

    usage_response = client.get("/api/usage")
    assert usage_response.status_code == 200
    assert usage_response.json()["total_tokens"] == 120
    assert usage_response.json()["estimated_cost_usd"] == 0.000027


def test_cleanup_old_runs_uses_backend_cleanup_listing():
    old_task = RunTask(
        run_id="old-run",
        owner_id="owner-1",
        status="completed",
        input_kind="file",
        requested_format="xlsx",
        current_stage="completed",
        created_at=datetime.now() - timedelta(days=10),
        updated_at=datetime.now() - timedelta(days=10),
    )

    class FakeStore:
        def __init__(self):
            self.deleted = []

        def list_runs_for_cleanup(self, cutoff, limit=100000):
            assert cutoff > datetime.now() - timedelta(days=4)
            assert limit == 100000
            return [old_task]

        def delete_run(self, run_id, owner_id):
            self.deleted.append((run_id, owner_id))
            return True

    store = FakeStore()

    deleted = cleanup_old_runs(store, max_age_days=3)

    assert deleted == 1
    assert store.deleted == [("old-run", "owner-1")]


def test_enforce_cache_size_limit_deletes_oldest_files():
    cache_dir = Path.cwd() / "data" / "test_cache_limit" / uuid4().hex / "cache"
    try:
        cache_dir.mkdir(parents=True)
        old = cache_dir / "old.bin"
        newer = cache_dir / "new.bin"
        old.write_bytes(b"a" * 900_000)
        newer.write_bytes(b"b" * 900_000)
        old_time = (datetime.now() - timedelta(days=2)).timestamp()
        new_time = datetime.now().timestamp()
        import os

        os.utime(old, (old_time, old_time))
        os.utime(newer, (new_time, new_time))

        deleted = enforce_cache_size_limit(cache_dir, max_mb=1)

        assert deleted == 1
        assert not old.exists()
        assert newer.exists()
    finally:
        shutil.rmtree(cache_dir.parent, ignore_errors=True)


def test_system_storage_and_volume_use_configured_cache_dir(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("MONGODB_URI", "")
    monkeypatch.setenv("MONGODB_URL", "")
    test_root = Path.cwd() / "data" / "test_storage" / uuid4().hex
    try:
        settings_path = test_root / "settings.yaml"
        cache_dir = test_root / "custom-cache"
        uploads_dir = test_root / "uploads"
        reports_dir = test_root / "reports"
        (cache_dir / "sources").mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "sources" / "source.html").write_bytes(b"x" * 1024)
        (uploads_dir / "input.pdf").write_bytes(b"u" * 2048)
        (reports_dir / "result.xlsx").write_bytes(b"r" * 4096)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            f"""
cache:
  dir: {cache_dir}
  max_total_mb: 300
storage:
  uploads_dir: {uploads_dir}
  reports_dir: {reports_dir}
auth:
  require_auth: false
queue:
  backend: local
runtime:
  task_store_backend: local
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("MARKET_SOURCE_SETTINGS", str(settings_path))

        from fastapi.testclient import TestClient

        from market_source_verification_agent.api import create_app

        import market_source_verification_agent.api as api_module

        original_load_settings = api_module.load_settings
        api_module.load_settings = lambda: original_load_settings(settings_path)
        try:
            client = TestClient(create_app())
        finally:
            api_module.load_settings = original_load_settings

        volume = client.get("/api/volume").json()
        storage = client.get("/api/system/storage").json()

        assert volume["cache_sources_mb"] > 0
        assert volume["uploads_mb"] > 0
        assert volume["reports_mb"] > 0
        assert storage["task_store_backend"] == "local"
        assert storage["mongodb_configured"] is False
        assert storage["mongodb_connected"] is False
        assert storage["volume"]["paths"]["cache"] == str(cache_dir)
    finally:
        shutil.rmtree(test_root, ignore_errors=True)
