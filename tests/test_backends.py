from market_source_verification_agent.backends import create_task_store
from market_source_verification_agent.config import Settings
from market_source_verification_agent.queueing import enqueue_run
from market_source_verification_agent.tasks import LocalTaskStore


def test_backend_can_be_forced_to_local(monkeypatch):
    monkeypatch.setenv("MONGODB_URL", "mongodb://example.invalid")
    settings = Settings.model_validate({"runtime": {"task_store_backend": "local"}})

    store = create_task_store(settings)

    assert isinstance(store, LocalTaskStore)


def test_enqueue_returns_false_without_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    settings = Settings.model_validate({"queue": {"backend": "auto"}})

    assert enqueue_run("run-id", "owner-id", settings) is False
