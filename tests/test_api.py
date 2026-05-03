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
