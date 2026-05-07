"""OpenAI token usage accounting and cost estimation."""

from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


# Standard API prices per 1M text tokens, from OpenAI pricing/model docs.
# gpt-4o-mini: input $0.15, cached input $0.075, output $0.60 per 1M tokens.
OPENAI_TEXT_PRICES_PER_1M = {
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "gpt-4o": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
    "gpt-4o-2024-08-06": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
}


def empty_usage_totals() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "by_model": {},
    }


class UsageAccumulator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals = empty_usage_totals()

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merge_usage(self._totals, entry)
            return deepcopy(self._totals)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._totals)


def record_openai_usage(settings: Any, model: str, response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    entry = usage_entry(model, usage)
    callback: Callable[[dict[str, Any]], None] | None = getattr(settings, "usage_callback", None)
    if callback is not None:
        callback(entry)
    return entry


def usage_entry(model: str, usage: Any) -> dict[str, Any]:
    input_tokens = _int_attr(usage, "prompt_tokens", "input_tokens")
    output_tokens = _int_attr(usage, "completion_tokens", "output_tokens")
    total_tokens = _int_attr(usage, "total_tokens") or input_tokens + output_tokens
    cached_input_tokens = _cached_input_tokens(usage)
    billable_input_tokens = max(input_tokens - cached_input_tokens, 0)
    prices = _prices_for_model(model)
    estimated_cost_usd = (
        billable_input_tokens * prices["input"]
        + cached_input_tokens * prices["cached_input"]
        + output_tokens * prices["output"]
    ) / 1_000_000
    return {
        "model": model,
        "calls": 1,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


def merge_usage(totals: dict[str, Any], entry: dict[str, Any]) -> None:
    model = str(entry.get("model") or "unknown")
    for key in ("calls", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        totals[key] = int(totals.get(key, 0)) + int(entry.get(key, 0))
    totals["estimated_cost_usd"] = float(totals.get("estimated_cost_usd", 0.0)) + float(
        entry.get("estimated_cost_usd", 0.0)
    )

    by_model = totals.setdefault("by_model", {})
    model_totals = by_model.setdefault(
        model,
        {
            "calls": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        },
    )
    for key in ("calls", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        model_totals[key] = int(model_totals.get(key, 0)) + int(entry.get(key, 0))
    model_totals["estimated_cost_usd"] = float(model_totals.get("estimated_cost_usd", 0.0)) + float(
        entry.get("estimated_cost_usd", 0.0)
    )


def compact_usage_summary(usage: dict[str, Any]) -> dict[str, Any]:
    summary = deepcopy(usage)
    summary["estimated_cost_usd"] = round(float(summary.get("estimated_cost_usd", 0.0)), 8)
    for model_totals in summary.get("by_model", {}).values():
        model_totals["estimated_cost_usd"] = round(float(model_totals.get("estimated_cost_usd", 0.0)), 8)
    return summary


_ledger_lock = threading.Lock()


def add_persistent_usage(settings: Any, entry: dict[str, Any]) -> dict[str, Any]:
    path = _usage_ledger_path(settings)
    with _ledger_lock:
        totals = read_persistent_usage(settings)
        merge_usage(totals, entry)
        totals = compact_usage_summary(totals)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(_json_dumps(totals), encoding="utf-8")
        tmp_path.replace(path)
        return totals


def read_persistent_usage(settings: Any) -> dict[str, Any]:
    path = _usage_ledger_path(settings)
    if not path.exists():
        return empty_usage_totals()
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        totals = empty_usage_totals()
        for key in ("calls", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            totals[key] = int(raw.get(key, 0) or 0)
        totals["estimated_cost_usd"] = float(raw.get("estimated_cost_usd", 0.0) or 0.0)
        by_model = raw.get("by_model", {})
        totals["by_model"] = by_model if isinstance(by_model, dict) else {}
        return compact_usage_summary(totals)
    except (OSError, ValueError, TypeError):
        return empty_usage_totals()


def _usage_ledger_path(settings: Any) -> Path:
    cache_dir = Path(getattr(getattr(settings, "cache", None), "dir", "data/cache"))
    if not cache_dir.is_absolute():
        try:
            from .config import ROOT

            cache_dir = ROOT / cache_dir
        except Exception:
            pass
    return cache_dir / "usage_totals.json"


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _prices_for_model(model: str) -> dict[str, float]:
    if model in OPENAI_TEXT_PRICES_PER_1M:
        return OPENAI_TEXT_PRICES_PER_1M[model]
    base = model.rsplit("-", 1)[0]
    return OPENAI_TEXT_PRICES_PER_1M.get(base, {"input": 0.0, "cached_input": 0.0, "output": 0.0})


def _int_attr(obj: Any, *names: str) -> int:
    for name in names:
        value = _get(obj, name)
        if isinstance(value, int):
            return value
    return 0


def _cached_input_tokens(usage: Any) -> int:
    details = _get(usage, "prompt_tokens_details") or _get(usage, "input_tokens_details")
    value = _get(details, "cached_tokens")
    return value if isinstance(value, int) else 0


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
