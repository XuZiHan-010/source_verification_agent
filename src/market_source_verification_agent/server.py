"""Console entry point for the local FastAPI service."""

from __future__ import annotations

import os

from .config import load_settings


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("source-verification-api requires uvicorn") from exc

    settings = load_settings()
    port = int(os.environ.get("PORT", settings.web.port))
    uvicorn.run(
        "market_source_verification_agent.api:app",
        host=settings.web.host,
        port=port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
