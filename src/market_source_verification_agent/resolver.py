"""Resolve source names and URL hints into fetchable source records."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .config import ROOT, Settings, load_source_tiers
from .schema import Claim, ResolvedSource

logger = logging.getLogger(__name__)


_BROWSER_UA_PRIMARY = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_BROWSER_UA_FALLBACK = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
_DEFAULT_HEADERS = {
    "User-Agent": _BROWSER_UA_PRIMARY,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept-Encoding": "gzip, deflate, br",
}

_NAV_KEYWORDS = (
    "首页", "登录", "搜索", "退出", "邮箱", "无障碍", "关于我们", "联系我们",
    "版权所有", "Copyright", "ICP", "京公网安备", "返回顶部", "公司简介",
    "企业文化", "公司动态", "党建工作", "投资者关系", "加入我们",
    "简体", "繁体", "English", "EN", "简 ", "繁 ",
)
_MIN_GOOD_LENGTH = 200
_MAX_NAV_RATIO = 0.30
_MIN_GOOD_PARAGRAPHS = 3


# ── LLM extract budget (per process) ─────────────────────────────────────────
_llm_extract_lock = threading.Lock()
_llm_extract_calls = 0


def _llm_extract_budget_remaining(settings: Settings) -> bool:
    cap = getattr(settings.limits, "llm_extract_max_calls_per_run", 50)
    with _llm_extract_lock:
        return _llm_extract_calls < cap


def _llm_extract_consume() -> int:
    global _llm_extract_calls
    with _llm_extract_lock:
        _llm_extract_calls += 1
        return _llm_extract_calls


def get_llm_extract_calls() -> int:
    with _llm_extract_lock:
        return _llm_extract_calls


def reset_llm_extract_calls() -> None:
    global _llm_extract_calls
    with _llm_extract_lock:
        _llm_extract_calls = 0


# ── Public API ────────────────────────────────────────────────────────────────

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
    if fetched["status"] != "ok":
        for fallback in claim.extra_source_urls:
            fallback_url = _normalise_url(fallback)
            if not fallback_url:
                continue
            fallback_fetched = _fetch_and_cache(fallback_url, settings)
            if fallback_fetched["status"] == "ok":
                url = fallback_url
                fetched = fallback_fetched
                method = "hyperlink"
                break
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


# ── Fetch with retry + archive.org mirror ────────────────────────────────────

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

    direct = _http_get_with_retry(url)
    if direct["status"] == "ok":
        return _persist(direct["body"], direct["content_type_header"], cache_path, suffix, meta)

    # Archive.org mirror fallback for forbidden/timeout/5xx HTML pages.
    if suffix == ".html" and direct["status"] in {"forbidden", "timeout", "server_error"}:
        archived = _http_get_with_retry(_archive_url(url), max_attempts=2)
        if archived["status"] == "ok":
            persisted = _persist(archived["body"], archived["content_type_header"], cache_path, suffix, meta)
            persisted["mirror"] = "archive.org"
            return persisted

    meta["status"] = direct["status"] if direct["status"] != "server_error" else "forbidden"
    if meta["status"] == "404":
        meta["path"] = None
    else:
        meta["path"] = None
    return meta


def _persist(body: bytes, content_type_header: str, cache_path: Path, suffix: str, meta: dict) -> dict:
    cache_path.write_bytes(body)
    is_pdf = "pdf" in (content_type_header or "").lower() or suffix == ".pdf"
    meta["content_type"] = "pdf" if is_pdf else "html"
    meta["hash"] = hashlib.sha256(body).hexdigest()
    if not is_pdf:
        try:
            meta["title"] = _extract_title(body.decode("utf-8", errors="ignore"))
        except Exception:
            meta["title"] = None
    return meta


def _http_get_with_retry(url: str, max_attempts: int = 3) -> dict:
    """GET with browser UA, exponential backoff, and UA swap on 403/429."""
    last_status = "timeout"
    headers = dict(_DEFAULT_HEADERS)
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
                response = client.get(url)
            status_code = response.status_code
            if status_code == 200:
                return {
                    "status": "ok",
                    "body": response.content,
                    "content_type_header": response.headers.get("content-type", ""),
                }
            if status_code == 404:
                return {"status": "404", "body": b"", "content_type_header": ""}
            if status_code in {401, 403, 429}:
                last_status = "forbidden"
                # UA swap once
                headers["User-Agent"] = _BROWSER_UA_FALLBACK if headers["User-Agent"] == _BROWSER_UA_PRIMARY else _BROWSER_UA_PRIMARY
            elif 500 <= status_code < 600:
                last_status = "server_error"
            else:
                last_status = "forbidden"
        except httpx.TimeoutException:
            last_status = "timeout"
        except httpx.HTTPError as exc:
            logger.debug("HTTPError fetching %s: %s", url, exc)
            last_status = "forbidden"
        if attempt < max_attempts - 1:
            time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.3))
    return {"status": last_status, "body": b"", "content_type_header": ""}


def _archive_url(url: str) -> str:
    return f"https://web.archive.org/web/2024/{quote(url, safe=':/?&=._-#%')}"


# ── Cached source body → main text ───────────────────────────────────────────

def load_cached_source_text(source: ResolvedSource, settings: Settings | None = None) -> str:
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
    return _extract_html_main(raw, source.url or "", settings or Settings())


# ── Multi-extractor voting + LLM fallback ────────────────────────────────────

def _extract_html_main(html: str, url: str, settings: Settings) -> str:
    if not html:
        return ""

    candidates: list[tuple[str, float, str]] = []  # (text, score, source_name)

    # 1. trafilatura
    text = _try_trafilatura(html)
    if text:
        candidates.append((text, _score_extraction(text), "trafilatura"))

    # 2. readability-lxml
    text = _try_readability(html)
    if text:
        candidates.append((text, _score_extraction(text), "readability"))

    # 3. goose3
    text = _try_goose3(html, url)
    if text:
        candidates.append((text, _score_extraction(text), "goose3"))

    # Pick best from algorithmic extractors
    best = max(candidates, key=lambda item: item[1], default=None)
    if best and best[1] >= 1.0:
        return best[0]

    # 4. LLM fallback (only when budget remains and algorithmic results are weak)
    if _llm_extract_budget_remaining(settings):
        llm_text = _try_llm_extract(html, settings)
        if llm_text:
            llm_score = _score_extraction(llm_text)
            if best is None or llm_score > best[1]:
                return llm_text
            return best[0]

    if best and best[0]:
        return best[0]

    # Last-resort regex strip
    return _strip_html(html)


def _score_extraction(text: str) -> float:
    """Score 0..2: 1 length component + 1 quality component (1 - nav_ratio)."""
    text = (text or "").strip()
    if not text:
        return 0.0
    length = len(text)
    if length < 50:
        return 0.0
    paragraphs = [p for p in re.split(r"\n+", text) if len(p.strip()) >= 20]
    para_count = len(paragraphs)
    nav_chars = sum(text.count(kw) * len(kw) for kw in _NAV_KEYWORDS)
    nav_ratio = min(1.0, nav_chars / max(1, length))

    length_component = min(1.0, length / max(_MIN_GOOD_LENGTH, 1))
    quality_component = max(0.0, 1.0 - nav_ratio / max(_MAX_NAV_RATIO, 1e-6))
    paragraph_bonus = min(0.3, para_count / max(_MIN_GOOD_PARAGRAPHS * 4, 1))

    if length < _MIN_GOOD_LENGTH and nav_ratio > _MAX_NAV_RATIO:
        return 0.0
    return length_component + quality_component * 0.8 + paragraph_bonus


def _try_trafilatura(html: str) -> str:
    try:
        import trafilatura

        extracted = trafilatura.extract(html, include_tables=True, favor_recall=True)
        return (extracted or "").strip()
    except Exception as exc:
        logger.debug("trafilatura failed: %s", exc)
        return ""


def _try_readability(html: str) -> str:
    try:
        from readability import Document

        doc = Document(html)
        summary_html = doc.summary(html_partial=True) or ""
        return _strip_html(summary_html).strip()
    except Exception as exc:
        logger.debug("readability failed: %s", exc)
        return ""


def _try_goose3(html: str, url: str) -> str:
    try:
        from goose3 import Goose

        with Goose({"enable_image_fetching": False}) as g:
            article = g.extract(raw_html=html, url=url or None)
            return (article.cleaned_text or "").strip()
    except Exception as exc:
        logger.debug("goose3 failed: %s", exc)
        return ""


def _try_llm_extract(html: str, settings: Settings) -> str:
    try:
        from openai import OpenAI

        from .prompts import HTML_EXTRACT_SYSTEM, HTML_EXTRACT_USER

        cleaned = _prune_html_for_llm(html)
        if not cleaned.strip():
            return ""

        model = getattr(settings.models, "extractor", "gpt-5-mini")
        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": HTML_EXTRACT_SYSTEM},
                {"role": "user", "content": HTML_EXTRACT_USER.format(html=cleaned[:30000])},
            ],
            max_completion_tokens=4000,
        )
        _llm_extract_consume()
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("LLM extract fallback failed: %s", exc)
        return ""


def _prune_html_for_llm(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<(?:nav|header|footer|aside|form|noscript|svg|iframe).*?</(?:nav|header|footer|aside|form|noscript|svg|iframe)>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

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
