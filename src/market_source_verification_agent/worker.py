"""RQ worker entry points."""

from __future__ import annotations

from .backends import create_task_store
from .config import load_settings
from .tasks import execute_run


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def run_worker(run_id: str, owner_id: str):
    _load_dotenv()
    settings = load_settings()
    store = create_task_store(settings)
    return execute_run(run_id, owner_id, store).model_dump(mode="json")


def main() -> int:
    _load_dotenv()
    try:
        from redis import Redis
        from rq import Worker
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("worker requires redis and rq dependencies") from exc

    from .queueing import redis_url

    settings = load_settings()
    url = redis_url(settings)
    if not url:
        raise RuntimeError(f"Missing Redis URL env var: {settings.queue.redis_url_env}")
    connection = Redis.from_url(url)
    worker = Worker([settings.queue.default_queue], connection=connection)
    worker.work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
