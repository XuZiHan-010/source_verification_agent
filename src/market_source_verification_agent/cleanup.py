"""Periodic cleanup to prevent memory exhaustion and disk bloat."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def get_dir_size(path: Path) -> int:
    """Get total size of directory in bytes."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception as exc:
        logger.error(f"Failed to calculate dir size for {path}: {exc}")
    return total


def cleanup_old_runs(store, max_age_days: int = 7, dry_run: bool = False) -> int:
    """Delete runs older than max_age_days to free memory/disk."""
    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0

    try:
        # List all runs for all users (use a dummy owner_id for local storage)
        tasks = store.list_runs("*", limit=100000, offset=0)
        for task in tasks:
            if task.created_at and task.created_at < cutoff:
                if not dry_run:
                    store.delete_run(task.run_id, task.owner_id)
                deleted += 1
                logger.info(f"cleanup: deleted old run {task.run_id}")
    except Exception as exc:
        logger.error(f"cleanup_old_runs failed: {exc}")

    return deleted


def cleanup_cache_dir(cache_dir: Path, max_age_days: int = 7, dry_run: bool = False) -> int:
    """Remove cached files older than max_age_days (aggressive for Railway)."""
    if not cache_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0

    for cache_file in cache_dir.rglob("*"):
        if cache_file.is_file():
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if mtime < cutoff:
                if not dry_run:
                    try:
                        cache_file.unlink()
                        deleted += 1
                        logger.info(f"cleanup: deleted cache {cache_file.name}")
                    except Exception as exc:
                        logger.error(f"Failed to delete cache {cache_file}: {exc}")

    return deleted


def cleanup_uploads_dir(uploads_dir: Path, max_age_days: int = 1, dry_run: bool = False) -> int:
    """Remove uploaded files older than max_age_days (aggressive for Railway)."""
    if not uploads_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0

    for upload_file in uploads_dir.rglob("*"):
        if upload_file.is_file():
            mtime = datetime.fromtimestamp(upload_file.stat().st_mtime)
            if mtime < cutoff:
                if not dry_run:
                    try:
                        upload_file.unlink()
                        deleted += 1
                        logger.info(f"cleanup: deleted upload {upload_file.name}")
                    except Exception as exc:
                        logger.error(f"Failed to delete upload {upload_file}: {exc}")

    return deleted


def cleanup_reports_dir(reports_dir: Path, max_age_days: int = 7, dry_run: bool = False) -> int:
    """Remove old report directories to save disk space (more aggressive for Railway)."""
    if not reports_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0

    for run_dir in reports_dir.iterdir():
        if run_dir.is_dir():
            mtime = datetime.fromtimestamp(run_dir.stat().st_mtime)
            if mtime < cutoff:
                if not dry_run:
                    try:
                        shutil.rmtree(run_dir)
                        deleted += 1
                        logger.info(f"cleanup: deleted report dir {run_dir.name}")
                    except Exception as exc:
                        logger.error(f"Failed to delete report dir {run_dir}: {exc}")

    return deleted


def run_cleanup(settings, dry_run: bool = False) -> dict:
    """Run all cleanup tasks and report disk usage."""
    from .backends import create_task_store

    logger.info("cleanup: starting scheduled cleanup")
    results = {
        "deleted_runs": 0,
        "deleted_cache": 0,
        "deleted_uploads": 0,
        "deleted_reports": 0,
        "disk_usage_mb": {},
    }

    try:
        store = create_task_store(settings)

        # Railway: Use aggressive cleanup (3 days for runs, 3 days for cache)
        # This prevents volume from filling up
        results["deleted_runs"] = cleanup_old_runs(store, max_age_days=3, dry_run=dry_run)

        # Clean old cache files (keep last 3 days)
        cache_dir = Path(settings.storage.cache_dir if hasattr(settings.storage, "cache_dir")
                        else "data/cache")
        results["deleted_cache"] = cleanup_cache_dir(cache_dir, max_age_days=3, dry_run=dry_run)
        results["disk_usage_mb"]["cache"] = get_dir_size(cache_dir) / 1024 / 1024

        # Clean old uploads (keep last 12 hours)
        uploads_dir = Path(settings.storage.uploads_dir)
        results["deleted_uploads"] = cleanup_uploads_dir(uploads_dir, max_age_days=0.5, dry_run=dry_run)
        results["disk_usage_mb"]["uploads"] = get_dir_size(uploads_dir) / 1024 / 1024

        # Clean old reports (keep last 3 days)
        reports_dir = Path(settings.storage.reports_dir)
        results["deleted_reports"] = cleanup_reports_dir(reports_dir, max_age_days=3, dry_run=dry_run)
        results["disk_usage_mb"]["reports"] = get_dir_size(reports_dir) / 1024 / 1024

        total_mb = sum(results["disk_usage_mb"].values())
        results["disk_usage_mb"]["total"] = total_mb
        logger.info(f"cleanup: completed - {results}")

        # Warn if disk usage is high
        if total_mb > 500:
            logger.warning(f"cleanup: high disk usage detected: {total_mb:.1f} MB")
    except Exception as exc:
        logger.error(f"cleanup failed: {exc}", exc_info=True)

    return results
