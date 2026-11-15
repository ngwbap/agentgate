"""Thin wrapper around the Claude API: structured JSON answers + cost accounting.

The AI only does language work (understanding requests, judging product relevance).
Every decision about limits, ranking and approval is made by plain, testable rules.
"""
from __future__ import annotations

import hashlib
import json
import threading

from . import config

# USD per million tokens (input, output) - shown in the UI so every request's cost is visible.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

_client = None
_lock = threading.Lock()
usage_totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "cached_answers": 0}

# The same question gets the same answer. Language models can word an answer differently each time, and a
# buyer who asks twice must not get two different "Best" plans. Answers are kept by a fingerprint of the exact
# question (model, instructions, text, expected shape), in memory and in a small file, so a repeat costs
# nothing and takes no time. The prompt that reads a request contains today's date, so it renews daily.
CACHE_MAX = 400
_cache: dict[str, dict] | None = None


def _cache_load() -> dict[str, dict]:
    global _cache
    if _cache is None:
        _cache = {}
        if config.PERSIST and config.LLM_CACHE_FILE.exists():
            try:
                _cache = json.loads(config.LLM_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _cache = {}
    return _cache


def _cache_save() -> None:
    if not config.PERSIST:
        return
    try:
        tmp = config.LLM_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache), encoding="utf-8")
        tmp.replace(config.LLM_CACHE_FILE)
    except OSError:
        pass


def clear_cache() -> None:
    global _cache
    _cache = {}
    _cache_save()


class LLMUnavailable(Exception):
    pass


def available() -> bool:
    return bool(__import__("os").environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        if not available():
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
            _client = anthropic.Anthropic(timeout=60.0, max_retries=2)
        except Exception as exc:
            raise LLMUnavailable(f"Claude library can't load: {exc}") from exc
    return _client


def ask_json(system: str, user: str, schema: dict, *, model: str | None = None,
             effort: str | None = None, max_tokens: int = 8000) -> tuple[dict, dict]:
    """Ask Claude for JSON matching `schema`. Returns (data, usage_info). Raises LLMUnavailable on any failure."""
    try:
        import anthropic
    except Exception as exc:          # e.g. a Windows security policy blocking one of the library's compiled files
        raise LLMUnavailable(f"Claude library can't load: {exc}") from exc
    model = model or config.CLAUDE_MODEL
    effort = effort or config.CLAUDE_EFFORT
    fingerprint = hashlib.sha256(json.dumps([model, effort, system, user, schema], sort_keys=True).encode()).hexdigest()
    with _lock:
        hit = _cache_load().get(fingerprint)
        if hit:
            usage_totals["cached_answers"] += 1
            return json.loads(json.dumps(hit["data"])), {"model": hit["model"], "input_tokens": 0, "output_tokens": 0,
                                                         "usd": 0.0, "cached": True}
    client = _get_client()
    params = dict(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    try:
        try:
            # Server-side fallback: if a request is declined, the API retries it on another model.
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        except anthropic.BadRequestError:
            resp = client.messages.create(**params)
    except anthropic.APIError as exc:
        raise LLMUnavailable(f"Claude API error: {exc.__class__.__name__}") from exc
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise LLMUnavailable(f"Claude stopped: {resp.stop_reason}")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable("Claude returned invalid JSON") from exc
    pin, pout = PRICES.get(resp.model, PRICES.get(model, (0, 0)))
    usd = (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
    info = {"model": resp.model, "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens, "usd": round(usd, 5)}
    with _lock:
        usage_totals["calls"] += 1
        usage_totals["input_tokens"] += resp.usage.input_tokens
        usage_totals["output_tokens"] += resp.usage.output_tokens
        usage_totals["usd"] = round(usage_totals["usd"] + usd, 5)
        cache = _cache_load()
        if len(cache) >= CACHE_MAX:                      # drop the oldest answers
            for old in list(cache)[: CACHE_MAX // 4]:
                del cache[old]
        cache[fingerprint] = {"data": data, "model": resp.model}
        _cache_save()
    return data, info
