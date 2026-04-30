"""CLI entry point kept for pyproject compatibility."""

from __future__ import annotations

import argparse
import json
import sys

from market_source_verification_agent.errors import AgentError
from market_source_verification_agent.orchestrator import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify-sources", description="Verify source authenticity and tier cited sources.")
    parser.add_argument("input", help="Input PDF, DOCX, TXT, or Markdown file")
    parser.add_argument("--fmt", choices=["xlsx", "md", "html", "json"], default="xlsx")
    parser.add_argument("-o", "--output", help="Output report path")
    parser.add_argument("--detailed", action="store_true", help="Include evidence/detail column")
    parser.add_argument("--no-cache", action="store_true", help="Reserved for cache refresh compatibility")
    parser.add_argument("--config", help="Path to settings.yaml")
    parser.add_argument("--limit", type=int, help="Only process first N claims")
    args = parser.parse_args(argv)

    try:
        report = run(
            args.input,
            out_path=args.output,
            fmt=args.fmt,
            config=args.config,
            detailed=args.detailed,
            no_cache=args.no_cache,
            limit=args.limit,
        )
    except AgentError as exc:
        print(f"verify-sources failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
