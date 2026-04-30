"""Resolve source names and URL hints into fetchable source records."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import ROOT, Settings, load_source_tiers
from .schema import Claim, ResolvedSource


def resolve(claim: Claim, settings: Settings | None = None, source_tiers: dict | None = None) -> ResolvedSource:
    settings = settings or Settings()
    tiers = source_tiers or load_source_tiers()
    url = _normalise_url(claim.source_url_hint)
    method = "hyperlink" if url and claim.source_url_hint and claim.source_url_hint.startswith(("http://", "https://")) else None

    if not url:
        mapped_domain = _match_institution_domain(claim.source_name_raw, tiers)
        if mapped_domain:
            url = f"https://{mapped_domain}"
            method = "whitelist"

    if not url:
        domain_hint = _extract_domain(claim.source_name_raw)
        if domain_hint:
            url = f"https://{domain_hint}"
            method = "whitelist"

    if not url:
        return ResolvedSource(
            claim_id=claim.claim_id,
            resolution_method="failed",
            fetch_status="skipped",
        )

    fetched = _fetch_and_cache(url, settings)
    domain = _domain(url)
    return ResolvedSource(
        claim_id=claim.claim_id,
        resolution_method=method or "whitelist",
        url=url,
        domain=domain,
        title=fetched["title"],
        fetch_status=fetched["status"],
        local_cache_path=fetched["path"],
        content_type=fetched["content_type"],
        content_hash=fetched["hash"],
    )


def _fetch_and_cache(url: str, settings: Settings) -> dict[str, str | None]:
    cache_dir = _abs_path(settings.cache.dir) / "sources"
    cache_dir.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    suffix = ".pdf" if url.lower().endswith(".pdf") else ".html"
    cache_path = cache_dir / f"{url_hash}{suffix}"
    meta = {"status": "ok", "path": str(cache_path), "content_type": "pdf" if suffix == ".pdf" else "html", "hash": None, "title": None}

    if cache_path.exists():
        body = cache_path.read_bytes()
        meta["hash"] = hashlib.sha256(body).hexdigest()
        meta["title"] = _extract_title(body.decode("utf-8", errors="ignore"))
        return meta

    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "MarketSourceVerificationAgent/0.1"}) as client:
            response = client.get(url)
        if response.status_code == 404:
            meta["status"] = "404"
            meta["path"] = None
            return meta
        if response.status_code in {401, 403}:
            meta["status"] = "forbidden"
            meta["path"] = None
            return meta
        response.raise_for_status()
        body = response.content
        cache_path.write_bytes(body)
        content_type = response.headers.get("content-type", "")
        meta["content_type"] = "pdf" if "pdf" in content_type or suffix == ".pdf" else "html"
        meta["hash"] = hashlib.sha256(body).hexdigest()
        meta["title"] = _extract_title(response.text)
        return meta
    except httpx.TimeoutException:
        meta["status"] = "timeout"
    except httpx.HTTPError:
        meta["status"] = "forbidden"
    meta["path"] = None
    return meta


def load_cached_source_text(source: ResolvedSource) -> str:
    if not source.local_cache_path:
        return ""
    path = Path(source.local_cache_path)
    if not path.exists():
        return ""
    if source.content_type == "pdf":
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:
            return ""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    try:
        import trafilatura

        extracted = trafilatura.extract(raw)
        return extracted or _strip_html(raw)
    except Exception:
        return _strip_html(raw)


def _normalise_url(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith(("http://", "https://")):
        return text
    domain = _extract_domain(text)
    return f"https://{domain}" if domain else None


def _extract_domain(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", text)
    return match.group(0).lower() if match else None


def _match_institution_domain(raw_name: str, tiers: dict) -> str | None:
    mapping = tiers.get("institution_to_domain", {})
    for name, domain in mapping.items():
        if name in raw_name:
            return domain
    return None


def _domain(url: str) -> str | None:
    return urlparse(url).netloc.lower().removeprefix("www.") or None


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if not match:
        return None
    return re.sub(r"\s+", " ", _strip_html(match.group(1))).strip()


def _strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _abs_path(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate
