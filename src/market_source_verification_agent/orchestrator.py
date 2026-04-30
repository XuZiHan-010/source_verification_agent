"""Synchronous local pipeline orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import classifier, extractor, ingestor, reporter, resolver, verifier
from .config import ROOT, Settings, load_settings, load_source_tiers
from .schema import Report


def run(
    input_path: str | Path,
    out_path: str | Path | None = None,
    fmt: str = "xlsx",
    config: Settings | str | Path | None = None,
    detailed: bool = False,
    no_cache: bool = False,
    limit: int | None = None,
) -> Report:
    started = datetime.now()
    settings = _settings(config)
    source_tiers = load_source_tiers()
    input_path = Path(input_path)
    fmt = fmt.lower()
    output_path = Path(out_path) if out_path else _default_output_path(input_path, fmt)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ir = ingestor.ingest(input_path)
    claims = extractor.extract_claims(ir, limit=limit or settings.limits.max_claims_per_run)

    with ThreadPoolExecutor(max_workers=settings.concurrency.fetch_workers) as pool:
        sources = list(pool.map(lambda claim: resolver.resolve(claim, settings, source_tiers), claims))

    with ThreadPoolExecutor(max_workers=settings.concurrency.llm_workers) as pool:
        verifies = list(pool.map(lambda pair: verifier.verify(*pair), zip(claims, sources)))

    classes = [classifier.classify(source, claim, source_tiers) for claim, source in zip(claims, sources)]
    verify_map = {item.claim_id: item for item in verifies}
    class_map = {item.claim_id: item for item in classes}

    payload = reporter.render(ir, claims, verify_map, class_map, fmt=fmt, detailed=detailed or settings.output.include_detail_column)  # type: ignore[arg-type]
    output_path.write_bytes(payload)
    finished = datetime.now()
    return Report(
        run_id=str(uuid4()),
        input_path=str(input_path),
        output_path=str(output_path),
        summary=reporter.summarize(verify_map, class_map),
        started_at=started,
        finished_at=finished,
        cost_usd=0.0,
        cache_hit_rate=0.0 if no_cache else 0.0,
    )


def _settings(config: Settings | str | Path | None) -> Settings:
    if isinstance(config, Settings):
        return config
    return load_settings(config)


def _default_output_path(input_path: Path, fmt: str) -> Path:
    return ROOT / "data" / "reports" / f"{input_path.stem}_verified.{fmt}"
