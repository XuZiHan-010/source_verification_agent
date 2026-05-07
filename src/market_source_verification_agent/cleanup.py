"""Periodic cleanup to prevent memory exhaustion and disk bloat."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT

logger = logging.getLogger(__name__)

DEFAULT_CACHE_MAX_MB = 300


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


def storage_paths(settings) -> dict[str, Path]:
    cache_dir = _abs_path(getattr(settings.cache, "dir", "data/cache"))
    uploads_dir = _abs_path(settings.storage.uploads_dir)
    reports_dir = _abs_path(settings.storage.reports_dir)
    return {
        "cache": cache_dir,
        "cache_sources": cache_dir / "sources",
        "cache_verify": cache_dir / "verify",
        "cache_classify": cache_dir / "classify",
        "uploads": uploads_dir,
        "reports": reports_dir,
    }


def volume_usage(settings) -> dict:
    paths = storage_paths(settings)
    usage = {f"{name}_mb": get_dir_size(path) / 1024 / 1024 for name, path in paths.items()}
    usage["cache_other_mb"] = max(
        usage["cache_mb"] - usage["cache_sources_mb"] - usage["cache_verify_mb"] - usage["cache_classify_mb"],
        0,
    )
    usage["total_mb"] = usage["cache_mb"] + usage["uploads_mb"] + usage["reports_mb"]
    usage["warning"] = usage["total_mb"] > 500
    usage["paths"] = {name: str(path) for name, path in paths.items()}
    return usage


def cleanup_old_runs(store, max_age_days: int = 7, dry_run: bool = False) -> int:
    """Delete runs older than max_age_days to free memory/disk."""
    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0

    try:
        if hasattr(store, "list_runs_for_cleanup"):
            tasks = store.list_runs_for_cleanup(cutoff, limit=100000)
        else:
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

    for cache_file in list(cache_dir.rglob("*")):
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
                else:
                    deleted += 1

    if not dry_run:
        remove_empty_dirs(cache_dir)

    return deleted


def cleanup_uploads_dir(uploads_dir: Path, max_age_days: int = 1, dry_run: bool = False) -> int:
    """Remove uploaded files older than max_age_days (aggressive for Railway)."""
    if not uploads_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0

    for upload_file in list(uploads_dir.rglob("*")):
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
                else:
                    deleted += 1

    if not dry_run:
        remove_empty_dirs(uploads_dir)

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
                else:
                    deleted += 1

    if not dry_run:
        remove_empty_dirs(reports_dir)

    return deleted


def enforce_cache_size_limit(cache_dir: Path, max_mb: int, dry_run: bool = False) -> int:
    if max_mb <= 0 or not cache_dir.exists():
        return 0
    max_bytes = max_mb * 1024 * 1024
    files: list[tuple[float, int, Path]] = []
    total = 0
    for path in cache_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        files.append((stat.st_mtime, stat.st_size, path))
    if total <= max_bytes:
        return 0

    deleted = 0
    for _, size, path in sorted(files, key=lambda item: item[0]):
        if total <= max_bytes:
            break
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                logger.error(f"Failed to delete cache for size cap {path}: {exc}")
                continue
        total -= size
        deleted += 1

    if not dry_run:
        remove_empty_dirs(cache_dir)
    return deleted


def count_cleanup_candidates(settings, max_age_days: int = 3) -> dict[str, int]:
    paths = storage_paths(settings)
    return {
        "cache": count_old_files(paths["cache"], max_age_days),
        "uploads": count_old_files(paths["uploads"], 0.5),
        "reports": count_old_dirs(paths["reports"], max_age_days),
    }


def count_old_files(path: Path, max_age_days: float) -> int:
    if not path.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    count = 0
    for item in path.rglob("*"):
        if item.is_file() and datetime.fromtimestamp(item.stat().st_mtime) < cutoff:
            count += 1
    return count


def count_old_dirs(path: Path, max_age_days: float) -> int:
    if not path.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    count = 0
    for item in path.iterdir():
        if item.is_dir() and datetime.fromtimestamp(item.stat().st_mtime) < cutoff:
            count += 1
    return count


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def run_cleanup(settings, dry_run: bool = False) -> dict:
    """Run all cleanup tasks and report disk usage."""
    from .backends import create_task_store

    logger.info("cleanup: starting scheduled cleanup")
    results = {
        "deleted_runs": 0,
        "deleted_cache": 0,
        "deleted_cache_for_size": 0,
        "deleted_uploads": 0,
        "deleted_reports": 0,
        "disk_usage_mb": {},
        "dry_run": dry_run,
        "ran_at": datetime.now().isoformat(),
    }

    try:
        store = create_task_store(settings)

        # Railway: Use aggressive cleanup (3 days for runs, 3 days for cache)
        # This prevents volume from filling up
        results["deleted_runs"] = cleanup_old_runs(store, max_age_days=3, dry_run=dry_run)

        paths = storage_paths(settings)

        # Clean old cache files (keep last 3 days)
        cache_dir = paths["cache"]
        results["deleted_cache"] = cleanup_cache_dir(cache_dir, max_age_days=3, dry_run=dry_run)
        cache_max_mb = int(getattr(settings.cache, "max_total_mb", DEFAULT_CACHE_MAX_MB) or DEFAULT_CACHE_MAX_MB)
        results["deleted_cache_for_size"] = enforce_cache_size_limit(cache_dir, cache_max_mb, dry_run=dry_run)

        # Clean old uploads (keep last 12 hours)
        uploads_dir = paths["uploads"]
        results["deleted_uploads"] = cleanup_uploads_dir(uploads_dir, max_age_days=0.5, dry_run=dry_run)

        # Clean old reports (keep last 3 days)
        reports_dir = paths["reports"]
        results["deleted_reports"] = cleanup_reports_dir(reports_dir, max_age_days=3, dry_run=dry_run)

        usage = volume_usage(settings)
        results["disk_usage_mb"] = {key.removesuffix("_mb"): value for key, value in usage.items() if key.endswith("_mb")}
        total_mb = usage["total_mb"]
        logger.info(f"cleanup: completed - {results}")

        # Warn if disk usage is high
        if total_mb > 500:
            logger.warning(f"cleanup: high disk usage detected: {total_mb:.1f} MB")
    except Exception as exc:
        logger.error(f"cleanup failed: {exc}", exc_info=True)

    return results


def _abs_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate
