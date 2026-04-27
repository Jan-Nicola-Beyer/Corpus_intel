"""Enrichment (P12) — sentiment, entities, stance via Claude Haiku + caching.

Respects CLAUDE.md: everything semantic goes through ai/claude_client.py.

Results are cached in `sessions/enrichment_cache.json`, keyed by (row_hash, kind).
Re-running enrichment is free when the cache is warm.

Each enrich_* function returns a dict of {row_hash: enrichment_dict} and writes
the cache back to disk at the end.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from corpus_intel.constants import SESSION_DIR
from corpus_intel.ai.claude_client import call_claude_cached, calc_cost, extract_json

log = logging.getLogger("corpus_intel.enrichment")

_CACHE_PATH = os.path.join(SESSION_DIR, "enrichment_cache.json")
_lock = threading.Lock()

_SENTIMENT_SYSTEM = (
    "You are a precise sentiment classifier for social-media posts. "
    "For each post, output exactly: label (one of positive, neutral, negative, mixed), "
    "polarity (float in [-1, 1]), and subjectivity (float in [0, 1]). "
    "Return a JSON array — one object per input, in order."
)

_ENTITIES_SYSTEM = (
    "You are a named-entity extractor. For each post, return a JSON array with "
    "one object per input, each having `entities`: a list of "
    "{name, type, mention_span: [start, end]}. Types ⊆ {PERSON, ORG, LOC, "
    "PRODUCT, EVENT, MISC}. Keep the original casing of `name`."
)

_STANCE_SYSTEM_TEMPLATE = (
    "You are a stance classifier. For each post, decide whether the author is "
    "for, against, or neutral toward the target: {target}. "
    "Return JSON array — one object per post, each with `stance` ∈ {{for, against, neutral, unclear}} "
    "and `confidence` ∈ [0,1]."
)


# ─── Cache ──────────────────────────────────────────────────────────────────
def _load_cache() -> Dict[str, Any]:
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, _CACHE_PATH)


def _cache_key(kind: str, row_hash: str, extra: str = "") -> str:
    return f"{kind}:{row_hash}:{extra}"


# ─── Shared batch runner ────────────────────────────────────────────────────
def _batch_call(texts: List[str], system: str, *, task: str = "classification",
                api_key: str = "", batch_size: int = 20, max_tokens: int = 4000) -> Tuple[List[Any], Dict[str, Any]]:
    out: List[Any] = []
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
    total_cost = 0.0
    model_id = ""

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        numbered = "\n\n".join(f"[{i+1}] {t}" for i, t in enumerate(batch))
        user_cache_block = "Use the same schema for every row. Output JSON only — no prose."
        user_tail = f"Classify these {len(batch)} posts:\n\n{numbered}"
        text, usage, model_id = call_claude_cached(system, user_cache_block, user_tail, task=task, api_key=api_key, max_tokens=max_tokens)
        for k, v in usage.items():
            total_usage[k] = total_usage.get(k, 0) + int(v or 0)
        total_cost += calc_cost(usage, model_id)
        parsed = extract_json(text)
        if isinstance(parsed, list):
            out.extend(parsed)
        elif isinstance(parsed, dict) and "items" in parsed:
            out.extend(parsed["items"])
        else:
            log.warning("enrichment: unexpected response shape %r", text[:200])
            out.extend([{} for _ in batch])
    return out, {"usage": total_usage, "cost_usd": round(total_cost, 6), "model": model_id}


# ─── Helpers ────────────────────────────────────────────────────────────────
def _ensure_row_id(df: pd.DataFrame, row_id_col: str, text_col: str) -> pd.DataFrame:
    """Return a copy of df with a usable row_id_col.
    If `row_id_col` is missing, fall back to `post_id`, then to a sha1(text+index)."""
    if row_id_col in df.columns:
        return df
    out = df.copy()
    if "post_id" in out.columns:
        out[row_id_col] = out["post_id"].astype(str)
        return out
    series = out.get(text_col, pd.Series([""] * len(out))).fillna("").astype(str)
    out[row_id_col] = [hashlib.sha1(f"{i}:{t}".encode("utf-8")).hexdigest()[:16]
                       for i, t in enumerate(series.tolist())]
    return out


# ─── Public API ─────────────────────────────────────────────────────────────
def enrich_sentiment(df: pd.DataFrame, *, api_key: str, text_col: str = "text",
                     row_id_col: str = "row_hash") -> Dict[str, Any]:
    """Return {row_hash: {label, polarity, subjectivity}} for every row."""
    df = _ensure_row_id(df, row_id_col, text_col)
    cache = _load_cache()
    rows = df[[row_id_col, text_col]].dropna()
    pending: List[Tuple[str, str]] = []
    result: Dict[str, Any] = {}
    for _, r in rows.iterrows():
        rid = str(r[row_id_col])
        key = _cache_key("sentiment", rid)
        if key in cache:
            result[rid] = cache[key]
        else:
            pending.append((rid, str(r[text_col])))

    call_meta: Dict[str, Any] = {}
    if pending:
        if not api_key:
            raise RuntimeError("No API key set for enrichment.")
        outputs, call_meta = _batch_call([t for _, t in pending], _SENTIMENT_SYSTEM, api_key=api_key)
        with _lock:
            for (rid, _), output in zip(pending, outputs):
                if not isinstance(output, dict):
                    continue
                payload = {
                    "label": str(output.get("label") or ""),
                    "polarity": float(output.get("polarity") or 0.0),
                    "subjectivity": float(output.get("subjectivity") or 0.0),
                }
                result[rid] = payload
                cache[_cache_key("sentiment", rid)] = payload
            _save_cache(cache)
    return {"result": result, "meta": call_meta}


def enrich_entities(df: pd.DataFrame, *, api_key: str, text_col: str = "text",
                    row_id_col: str = "row_hash") -> Dict[str, Any]:
    df = _ensure_row_id(df, row_id_col, text_col)
    cache = _load_cache()
    rows = df[[row_id_col, text_col]].dropna()
    pending: List[Tuple[str, str]] = []
    result: Dict[str, Any] = {}
    for _, r in rows.iterrows():
        rid = str(r[row_id_col])
        key = _cache_key("entities", rid)
        if key in cache:
            result[rid] = cache[key]
        else:
            pending.append((rid, str(r[text_col])))

    call_meta: Dict[str, Any] = {}
    if pending:
        if not api_key:
            raise RuntimeError("No API key set for enrichment.")
        outputs, call_meta = _batch_call([t for _, t in pending], _ENTITIES_SYSTEM, api_key=api_key)
        with _lock:
            for (rid, _), output in zip(pending, outputs):
                if not isinstance(output, dict):
                    continue
                payload = {"entities": output.get("entities") or []}
                result[rid] = payload
                cache[_cache_key("entities", rid)] = payload
            _save_cache(cache)
    return {"result": result, "meta": call_meta}


def enrich_stance(df: pd.DataFrame, target: str, *, api_key: str,
                  text_col: str = "text", row_id_col: str = "row_hash") -> Dict[str, Any]:
    target = (target or "").strip()
    if not target:
        raise ValueError("stance enrichment needs a target")
    system = _STANCE_SYSTEM_TEMPLATE.format(target=target)
    target_hash = hashlib.sha1(target.lower().encode()).hexdigest()[:10]
    df = _ensure_row_id(df, row_id_col, text_col)
    cache = _load_cache()
    rows = df[[row_id_col, text_col]].dropna()
    pending: List[Tuple[str, str]] = []
    result: Dict[str, Any] = {}
    for _, r in rows.iterrows():
        rid = str(r[row_id_col])
        key = _cache_key("stance", rid, target_hash)
        if key in cache:
            result[rid] = cache[key]
        else:
            pending.append((rid, str(r[text_col])))

    call_meta: Dict[str, Any] = {}
    if pending:
        if not api_key:
            raise RuntimeError("No API key set for enrichment.")
        outputs, call_meta = _batch_call([t for _, t in pending], system, api_key=api_key)
        with _lock:
            for (rid, _), output in zip(pending, outputs):
                if not isinstance(output, dict):
                    continue
                payload = {
                    "stance": str(output.get("stance") or "unclear"),
                    "confidence": float(output.get("confidence") or 0.0),
                    "target": target,
                }
                result[rid] = payload
                cache[_cache_key("stance", rid, target_hash)] = payload
            _save_cache(cache)
    return {"result": result, "meta": call_meta}


def detect_language(df: pd.DataFrame, *, text_col: str = "text",
                    out_col: str = "_lang_iso") -> pd.DataFrame:
    """Best-effort language detection with a regex fallback — zero API cost.
    For high-accuracy detection, pass texts through enrich_sentiment which handles
    multiple languages natively, or install `langdetect`."""
    try:
        from langdetect import detect_langs  # type: ignore
        def _det(t):
            try:
                langs = detect_langs(str(t))
                return str(langs[0].lang) if langs else ""
            except Exception:
                return ""
        df = df.copy()
        df[out_col] = df[text_col].fillna("").map(_det)
        return df
    except Exception:
        df = df.copy()
        # Crude heuristic: if text is mostly non-ASCII, tag "other"; else "en"
        def _guess(t):
            s = str(t or "")
            if not s: return ""
            nonascii = sum(1 for ch in s if ord(ch) > 127)
            return "other" if nonascii > len(s) * 0.3 else "en"
        df[out_col] = df[text_col].map(_guess)
        return df
