"""Startup and cleanup routine for Railway deployment."""

import logging

logger = logging.getLogger(__name__)


def run_startup_cleanup():
    """Run cleanup on application startup to prevent volume overflow."""
    from .cleanup import run_cleanup
    from .config import load_settings

    try:
        logger.info("Running startup cleanup...")
        settings = load_settings()
        results = run_cleanup(settings, dry_run=False)
        logger.info(f"Startup cleanup completed: {results}")
    except Exception as exc:
        logger.error(f"Startup cleanup failed: {exc}", exc_info=True)
