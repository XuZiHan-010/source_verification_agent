"""Redis/RQ queue integration for background run execution."""

from __future__ import annotations

import os

from .config import Settings, load_settings


def enqueue_run(run_id: str, owner_id: str, settings: Settings | None = None) -> bool:
    settings = settings or load_settings()
    if settings.queue.backend == "local":
        return False
    redis_url = os.getenv(settings.queue.redis_url_env)
    if not redis_url:
        return False
    try:
        from redis import Redis
        from rq import Queue
    except ImportError:
        return False

    try:
        connection = Redis.from_url(redis_url)
        connection.ping()
        queue = Queue(settings.queue.default_queue, connection=connection)
        queue.enqueue(
            "market_source_verification_agent.worker.run_worker",
            run_id,
            owner_id,
            job_timeout=settings.queue.job_timeout,
            result_ttl=settings.queue.result_ttl,
            failure_ttl=settings.queue.failure_ttl,
        )
        return True
    except Exception:
        if settings.queue.backend == "redis-rq":
            raise
        return False


def redis_url(settings: Settings | None = None) -> str | None:
    settings = settings or load_settings()
    return os.getenv(settings.queue.redis_url_env)
