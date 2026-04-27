"""Unified Claude API client — ALL Anthropic calls go through here (CLAUDE.md rule).

Phase 1: minimal client used by the column-mapping fallback.
Phase 5: extended with prompt caching, usage accounting, and retry/backoff.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from corpus_intel.constants import AI_MODELS


# Claude pricing (USD per million tokens). Source: anthropic.com pricing page.
# Kept here so callers can estimate cost without hitting the API.
MODEL_PRICING = {
    # Haiku 4.5
    "claude-haiku-4-5-20251001": {
        "input":         0.80,
        "output":        4.00,
        "cache_write":   1.00,
        "cache_read":    0.08,
    },
    # Sonnet 4.6
    "claude-sonnet-4-6": {
        "input":         3.00,
        "output":       15.00,
        "cache_write":   3.75,
        "cache_read":    0.30,
    },
}

log = logging.getLogger("corpus_intel.ai")


class ClaudeClientError(RuntimeError):
    """Raised when the Claude call fails in a way the caller should surface."""


def _resolve_model(task: str) -> str:
    return AI_MODELS.get(task, AI_MODELS["mapping"])


def calc_cost(usage: Dict[str, int], model: str) -> float:
    """USD cost from a usage dict {input_tokens, output_tokens, cache_read, cache_creation}."""
    p = MODEL_PRICING.get(model)
    if not p:
        return 0.0
    out  = (usage.get("output_tokens") or 0)       * p["output"]       / 1_000_000
    cr   = (usage.get("cache_read") or 0)          * p["cache_read"]   / 1_000_000
    cw   = (usage.get("cache_creation") or 0)      * p["cache_write"]  / 1_000_000
    # "Base" input = total input minus what was cache_read or cache_creation.
    base_in = max(0, (usage.get("input_tokens") or 0) - (usage.get("cache_read") or 0) - (usage.get("cache_creation") or 0))
    bi = base_in * p["input"] / 1_000_000
    return round(bi + cr + cw + out, 6)


def _usage_from_response(resp: Any) -> Dict[str, int]:
    u = getattr(resp, "usage", None)
    if not u:
        return {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
    return {
        "input_tokens":   int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens":  int(getattr(u, "output_tokens", 0) or 0),
        "cache_read":     int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_creation": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    }


def call_claude(
    system: str,
    user_msg: str,
    task: str = "mapping",
    api_key: str = "",
    max_tokens: int = 1000,
) -> str:
    """Send a message to Claude, retry on overload, return the text payload."""
    if not api_key:
        raise ClaudeClientError("No API key configured. Add one in Settings.")

    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - handled at env level
        raise ClaudeClientError(f"anthropic package not available: {e}") from e

    model = _resolve_model(task)
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)

    from corpus_intel.core import ai_health

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            ai_health.mark_up()
            return resp.content[0].text
        except anthropic.APIStatusError as e:
            msg = str(e).lower()
            if attempt < max_retries - 1 and ("overloaded" in msg or "rate_limit" in msg):
                wait = 4 * (attempt + 1)
                log.warning("Claude API %s; retry in %ds (%d/%d)", msg[:60], wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise ClaudeClientError(str(e)) from e
        except anthropic.APIConnectionError as e:
            ai_health.mark_down("unreachable", str(e))
            raise ClaudeClientError(f"Cannot reach api.anthropic.com: {e}") from e
        except Exception as e:
            raise ClaudeClientError(str(e)) from e

    raise ClaudeClientError("Claude call exhausted retries")


def call_claude_cached(
    system: str,
    user_cache_block: str,
    user_tail: str,
    task: str = "classification",
    api_key: str = "",
    max_tokens: int = 2000,
) -> Tuple[str, Dict[str, int], str]:
    """Send a message with prompt caching. `user_cache_block` is the stable part
    (codebook, examples, etc.) that should be cached across calls; `user_tail` is
    the per-call payload (batch of rows).

    Returns (text, usage_dict, model_id).
    """
    if not api_key:
        raise ClaudeClientError("No API key configured. Add one in Settings.")
    try:
        import anthropic
    except ImportError as e:
        raise ClaudeClientError(f"anthropic package not available: {e}") from e

    model = _resolve_model(task)
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0)

    user_content = [
        {"type": "text", "text": user_cache_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": user_tail},
    ]

    from corpus_intel.core import ai_health

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_content}],
            )
            ai_health.mark_up()
            text = resp.content[0].text if resp.content else ""
            return text, _usage_from_response(resp), model
        except anthropic.APIStatusError as e:
            msg = str(e).lower()
            if attempt < max_retries - 1 and ("overloaded" in msg or "rate_limit" in msg or "529" in msg):
                wait = 4 * (attempt + 1)
                log.warning("Claude cached-call %s; retry in %ds (%d/%d)", msg[:80], wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise ClaudeClientError(str(e)) from e
        except anthropic.APIConnectionError as e:
            ai_health.mark_down("unreachable", str(e))
            raise ClaudeClientError(f"Cannot reach api.anthropic.com: {e}") from e
        except Exception as e:
            raise ClaudeClientError(str(e)) from e
    raise ClaudeClientError("Claude cached call exhausted retries")


# ─── JSON helpers ────────────────────────────────────────────────────────────
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}|\[[\s\S]*\]")


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object/array out of a model response, tolerating prose."""
    if not text:
        return None
    text = text.strip()
    # Remove fenced code blocks
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
