"""AI-assisted classification — batch call Claude Haiku over a row scope.

The pipeline:
  1. Build a codebook prefix (cacheable — stays constant across a run).
  2. Split the scope into batches (default 20 rows/call).
  3. For each batch:
       a. Hit answer_cache first. Rows with a hit skip the LLM.
       b. Send the remaining rows to Claude via claude_client.call_claude_cached.
       c. Store returned answers back into the cache.
       d. Persist the chosen categories as tag entries on AppState.tags,
          recording coder="claude-haiku-4-5", source="ai", confidence=<float>.

Exclusion groups are respected at apply-time: if Claude returns two categories
from the same group for one row, we keep only the first.

The `run_classification` function is a generator that yields small progress
events, so the web layer can stream them as SSE.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from corpus_intel.ai.claude_client import (
    ClaudeClientError,
    calc_cost,
    call_claude_cached,
    extract_json,
    MODEL_PRICING,
)
from corpus_intel.ai.prompts import (
    CLASSIFICATION_BATCH_TEMPLATE,
    CLASSIFICATION_CODEBOOK_TEMPLATE,
    CLASSIFICATION_PROMPT_VERSION,
    CLASSIFICATION_SYSTEM_PROMPT,
    CODEBOOK_SUGGESTION_SYSTEM_PROMPT,
    CODEBOOK_SUGGESTION_USER_TEMPLATE,
)
from corpus_intel.app_state import AppState, ProvenanceEvent
from corpus_intel.constants import AI_MODELS
from corpus_intel.core import answer_cache
from corpus_intel.core.coding import _as_key, tag_row, CodingError
from corpus_intel.core.codebook import get_category

log = logging.getLogger("corpus_intel.ai_coding")

AI_CODER_NAME = "claude-haiku-4-5"
DEFAULT_BATCH_SIZE = 20
# 1 token ≈ 3.5 characters for English+EU languages. Good enough for preflight.
CHARS_PER_TOKEN = 3.5
# Rough guess — classification output is short JSON per row.
OUTPUT_TOKENS_PER_ROW = 40


# ─── Codebook prefix ────────────────────────────────────────────────────────
def format_codebook_prefix(cb: Any) -> str:
    """Render an active codebook as a plain-text block for the user cache prefix."""
    cats_lines: List[str] = []
    groups: Dict[str, List[str]] = {}
    for c in cb.categories:
        cid = c.get("cat_id", "")
        title = c.get("title", cid)
        desc = (c.get("description") or "").strip().replace("\n", " ")
        cats_lines.append(f"- {cid} — {title}: {desc or 'no description'}")
        g = (c.get("exclusion_group") or "").strip()
        if g:
            groups.setdefault(g, []).append(cid)
    group_lines = [f"- {g}: {', '.join(ids)}" for g, ids in groups.items()] or ["(none)"]
    return CLASSIFICATION_CODEBOOK_TEMPLATE.format(
        codebook_name=cb.name,
        codebook_version=cb.version or "1.0",
        categories="\n".join(cats_lines) or "(empty)",
        exclusion_groups="\n".join(group_lines),
    )


# ─── Preflight ──────────────────────────────────────────────────────────────
def preflight(
    rows: List[Tuple[int, str]],
    cb: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Estimate cost + latency + cache-hit rate before actually running."""
    model = model or AI_MODELS.get("classification", "claude-haiku-4-5-20251001")
    price = MODEL_PRICING.get(model, {})
    cat_ids = [c["cat_id"] for c in cb.categories]
    # Hit the cache first to see how many rows are already answered.
    keys = [answer_cache.row_key(t, cb.codebook_id, cb.version or "1.0",
                                 CLASSIFICATION_PROMPT_VERSION, cat_ids)
            for _, t in rows]
    cached = answer_cache.lookup_many(keys)
    hits = sum(1 for k in keys if k in cached)
    misses = len(rows) - hits

    # Tokens.
    codebook_prefix = format_codebook_prefix(cb)
    system_tokens   = math.ceil(len(CLASSIFICATION_SYSTEM_PROMPT) / CHARS_PER_TOKEN)
    codebook_tokens = math.ceil(len(codebook_prefix)            / CHARS_PER_TOKEN)
    per_row_chars   = sum(len(t or "") for _, t in rows if t) / max(1, len(rows))
    per_row_tokens  = math.ceil((per_row_chars + 30) / CHARS_PER_TOKEN)  # +overhead

    if misses == 0:
        return {
            "rows_total": len(rows),
            "cache_hits": hits,
            "rows_to_classify": 0,
            "batches": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "estimated_seconds": 0.0,
            "model": model,
            "batch_size": batch_size,
        }

    batches = math.ceil(misses / batch_size)
    # First batch pays cache-write on system + codebook; subsequent batches pay cache-read.
    cache_write_tokens = system_tokens + codebook_tokens
    cache_read_tokens  = cache_write_tokens * max(0, batches - 1)
    input_base_tokens  = misses * per_row_tokens + batches * 40  # +small wrapper
    output_tokens      = misses * OUTPUT_TOKENS_PER_ROW

    cost = 0.0
    if price:
        cost += cache_write_tokens   * price["cache_write"] / 1_000_000
        cost += cache_read_tokens    * price["cache_read"]  / 1_000_000
        cost += input_base_tokens    * price["input"]       / 1_000_000
        cost += output_tokens        * price["output"]      / 1_000_000

    # ~5s per batch is a reasonable point estimate for Haiku at 20 rows.
    est_seconds = batches * 5.0

    return {
        "rows_total": len(rows),
        "cache_hits": hits,
        "rows_to_classify": misses,
        "batches": batches,
        "estimated_input_tokens": int(cache_write_tokens + cache_read_tokens + input_base_tokens),
        "estimated_output_tokens": int(output_tokens),
        "estimated_cost_usd": round(cost, 4),
        "estimated_seconds": round(est_seconds, 1),
        "model": model,
        "batch_size": batch_size,
    }


# ─── Run ────────────────────────────────────────────────────────────────────
def _parse_response(text: str) -> List[Dict[str, Any]]:
    """Pull `{rows: [...]}` out of a Claude reply, tolerating extra prose."""
    payload = extract_json(text)
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    if isinstance(payload, list):
        return payload
    return []


def _apply_exclusion(cb: Any, cats: List[str]) -> List[str]:
    """Within each exclusion group, keep only the first cat seen."""
    seen_group: Dict[str, str] = {}
    kept: List[str] = []
    for cid in cats:
        cat = get_category(cb, cid)
        if cat is None:
            continue
        grp = (cat.get("exclusion_group") or "").strip()
        if grp:
            if grp in seen_group:
                continue
            seen_group[grp] = cid
        kept.append(cid)
    return kept


def _apply_answer_to_state(
    state: AppState,
    row_idx: int,
    cats: List[str],
    confidence: float,
    reason: str,
    codebook_id: str,
    model: str,
) -> int:
    """Write one row's AI tags into state.tags. Returns number of tag objects added."""
    # Strip any prior AI tags for this row+codebook — this is a fresh classification.
    key = _as_key(row_idx)
    existing = state.tags.get(key, [])
    kept = [e for e in existing
            if not (e.get("coder") == AI_CODER_NAME
                    and e.get("codebook_id") == codebook_id
                    and e.get("source") == "ai")]
    state.tags[key] = kept

    now = dt.datetime.now().isoformat(timespec="seconds")
    added = 0
    for cid in cats:
        new_tag = {
            "cat_id": cid,
            "coder": AI_CODER_NAME,
            "ts": now,
            "source": "ai",
            "codebook_id": codebook_id,
            "confidence": float(confidence),
            "reason": (reason or "")[:200],
            "model": model,
        }
        state.tags[key].append(new_tag)
        added += 1
    if not state.tags[key]:
        state.tags.pop(key, None)
    return added


def run_classification(
    state: AppState,
    rows: List[Tuple[int, str]],
    cb: Any,
    *,
    api_key: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Generator. Yields {type, ...} events for SSE streaming.

    Event types: start, cache, batch, error, done.
    """
    model = model or AI_MODELS.get("classification", "claude-haiku-4-5-20251001")
    cat_ids = [c["cat_id"] for c in cb.categories]
    cat_ids_set = set(cat_ids)
    codebook_prefix = format_codebook_prefix(cb)

    # 1. Bucket: cached vs. needs classification.
    keys = [answer_cache.row_key(t, cb.codebook_id, cb.version or "1.0",
                                 CLASSIFICATION_PROMPT_VERSION, cat_ids)
            for _, t in rows]
    cached_map = answer_cache.lookup_many(keys)
    to_classify: List[Tuple[int, str, str]] = []  # (row_idx, text, key)
    cache_hits = 0
    for (ri, text), k in zip(rows, keys):
        if k in cached_map:
            cache_hits += 1
        else:
            to_classify.append((ri, text, k))

    yield {
        "type": "start",
        "rows_total": len(rows),
        "cache_hits": cache_hits,
        "rows_to_classify": len(to_classify),
        "batches": math.ceil(len(to_classify) / batch_size) if to_classify else 0,
        "batch_size": batch_size,
        "model": model,
    }

    # 2. Apply cached results to state immediately.
    applied_rows = 0
    for (ri, text), k in zip(rows, keys):
        ans = cached_map.get(k)
        if not ans:
            continue
        cats = [c for c in (ans.get("cats") or []) if c in cat_ids_set]
        cats = _apply_exclusion(cb, cats)
        _apply_answer_to_state(state, ri, cats, ans.get("confidence") or 0.0,
                               ans.get("reason") or "", cb.codebook_id, ans.get("model") or model)
        applied_rows += 1

    if cache_hits:
        yield {"type": "cache", "applied": cache_hits}

    # 3. Batch remaining through the LLM.
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
    total_cost = 0.0
    for b_idx in range(0, len(to_classify), batch_size):
        batch = to_classify[b_idx:b_idx + batch_size]
        rows_json = json.dumps(
            [{"row_idx": ri, "text": (t or "")[:1500]} for ri, t, _ in batch],
            ensure_ascii=False,
        )
        user_tail = CLASSIFICATION_BATCH_TEMPLATE.format(rows_json=rows_json)

        t0 = time.time()
        try:
            text, usage, used_model = call_claude_cached(
                system=CLASSIFICATION_SYSTEM_PROMPT,
                user_cache_block=codebook_prefix,
                user_tail=user_tail,
                task="classification",
                api_key=api_key,
                max_tokens=min(4096, 80 + len(batch) * OUTPUT_TOKENS_PER_ROW * 2),
            )
        except ClaudeClientError as e:
            yield {"type": "error", "message": str(e), "batch_idx": b_idx // batch_size + 1}
            return

        batch_cost = calc_cost(usage, used_model)
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)
        total_cost += batch_cost

        answers = _parse_response(text)
        answer_by_row: Dict[int, Dict[str, Any]] = {}
        for a in answers:
            try:
                ri = int(a.get("row_idx"))
            except (TypeError, ValueError):
                continue
            answer_by_row[ri] = a

        # Apply to state + store in cache.
        store_payload: Dict[str, Dict[str, Any]] = {}
        applied_in_batch = 0
        for (ri, text_, k) in batch:
            a = answer_by_row.get(ri)
            if a is None:
                # Row missing from response — skip, don't poison the cache.
                continue
            raw_cats = [c for c in (a.get("cats") or []) if c in cat_ids_set]
            cats = _apply_exclusion(cb, raw_cats)
            confidence = float(a.get("confidence") or 0.0)
            reason = a.get("reason") or ""
            _apply_answer_to_state(state, ri, cats, confidence, reason, cb.codebook_id, used_model)
            store_payload[k] = {
                "cats": cats,
                "confidence": confidence,
                "reason": reason,
                "model": used_model,
            }
            applied_in_batch += 1
        answer_cache.store_many(store_payload, model=used_model)
        applied_rows += applied_in_batch

        yield {
            "type": "batch",
            "batch_idx": (b_idx // batch_size) + 1,
            "rows_in_batch": len(batch),
            "applied": applied_in_batch,
            "missing_in_response": len(batch) - applied_in_batch,
            "cost_usd": round(batch_cost, 5),
            "seconds": round(time.time() - t0, 2),
            "usage": usage,
            "cumulative_cost_usd": round(total_cost, 5),
            "applied_total": applied_rows,
        }

    state.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="ai_coding_run",
            params={
                "codebook_id": cb.codebook_id,
                "rows_total": len(rows),
                "rows_classified": len(to_classify),
                "cache_hits": cache_hits,
                "cost_usd": round(total_cost, 5),
                "model": model,
            },
        )
    )
    yield {
        "type": "done",
        "applied_total": applied_rows,
        "cache_hits": cache_hits,
        "rows_classified": len(to_classify),
        "cost_usd": round(total_cost, 5),
        "usage": total_usage,
        "model": model,
    }


# ─── Codebook suggestion ────────────────────────────────────────────────────
def suggest_codebook(
    goal: str,
    sample_texts: List[str],
    *,
    api_key: str,
) -> Dict[str, Any]:
    """Ask Sonnet to propose a codebook from a sample. Returns parsed JSON + meta."""
    sample_lines = []
    for i, t in enumerate(sample_texts[:80]):
        sample_lines.append(f"[{i + 1}] {str(t or '').strip()[:400]}")
    user_tail = CODEBOOK_SUGGESTION_USER_TEMPLATE.format(
        goal=(goal or "").strip() or "Identify the main types of posts in this data.",
        n=min(len(sample_texts), 80),
        sample="\n".join(sample_lines),
    )
    try:
        text, usage, model = call_claude_cached(
            system=CODEBOOK_SUGGESTION_SYSTEM_PROMPT,
            user_cache_block="(no shared context — single-shot suggestion)",
            user_tail=user_tail,
            task="codebook",
            api_key=api_key,
            max_tokens=2500,
        )
    except ClaudeClientError as e:
        return {"ok": False, "error": str(e)}
    payload = extract_json(text)
    if not isinstance(payload, dict) or "categories" not in payload:
        return {"ok": False, "error": "Could not parse JSON from model response.", "raw": text[:500]}
    return {
        "ok": True,
        "proposal": payload,
        "usage": usage,
        "cost_usd": round(calc_cost(usage, model), 5),
        "model": model,
    }
