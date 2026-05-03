"""Pydantic contracts shared by all pipeline stages."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Block(BaseModel):
    type: Literal["heading", "paragraph", "table", "list"]
    level: int | None = None
    text: str | None = None
    rows: list[list[str | None]] | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    hyperlinks: dict[str, str] = Field(default_factory=dict)
    footnotes: dict[int, str] = Field(default_factory=dict)


class IR(BaseModel):
    doc_id: str
    source_format: Literal["pdf", "docx", "txt", "md"]
    blocks: list[Block] = Field(default_factory=list)


class Claim(BaseModel):
    claim_id: str
    section_path: list[str] = Field(default_factory=list)
    metric: str | None = None
    value: str | None = None
    year: str | None = None
    region: str | None = None
    statement: str
    source_name_raw: str
    source_url_hint: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    extra_source_urls: list[str] = Field(default_factory=list)
    source_name_with_marks: str | None = None
    publish_time: str | None = None
    notes: str | None = None
    is_forecast: bool = False
    duplicate_count: int = 1
    duplicate_claim_ids: list[str] = Field(default_factory=list)


class ResolvedSource(BaseModel):
    claim_id: str
    resolution_method: Literal["hyperlink", "whitelist", "user_provided", "failed"]
    url: str | None = None
    domain: str | None = None
    title: str | None = None
    fetch_status: Literal["ok", "404", "forbidden", "timeout", "paywalled", "skipped"]
    local_cache_path: str | None = None
    content_type: Literal["html", "pdf", "text", "unknown"] | None = None
    content_hash: str | None = None


class VerifyResult(BaseModel):
    claim_id: str
    verdict: Literal[
        "supported",
        "partially_supported",
        "not_found",
        "contradicted",
        "not_verifiable",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str | None = None
    evidence_locator: str | None = None
    discrepancy: str | None = None
    reasoning: str
    source_details: list[dict[str, str | float | None]] = Field(default_factory=list)


class ClassifyResult(BaseModel):
    claim_id: str
    tier: Literal["A", "B", "C"]
    tier_reason: str
    matched_rule: str


class Report(BaseModel):
    run_id: str
    input_path: str
    output_path: str
    summary: dict[str, int] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    cost_usd: float | None = None
    cache_hit_rate: float = 0.0


class RunTask(BaseModel):
    run_id: str
    owner_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    input_kind: Literal["file", "text"]
    input_filename: str | None = None
    input_path: str | None = None
    requested_format: Literal["xlsx", "md", "html", "json"]
    detailed: bool = False
    total_claims: int = 0
    completed_claims: int = 0
    current_stage: Literal[
        "queued",
        "ingest",
        "extract",
        "resolve",
        "verify",
        "classify",
        "report",
        "completed",
        "failed",
    ] = "queued"
    summary: dict[str, int] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunEvent(BaseModel):
    event_id: str
    run_id: str
    stage: str
    level: Literal["info", "warning", "error"]
    message: str
    progress_current: int | None = None
    progress_total: int | None = None
    created_at: datetime


class Artifact(BaseModel):
    artifact_id: str
    run_id: str
    owner_id: str
    fmt: Literal["xlsx", "md", "html", "json"]
    storage_uri: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
