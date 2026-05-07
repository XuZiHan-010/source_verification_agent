from pathlib import Path
from uuid import uuid4

import pytest


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
