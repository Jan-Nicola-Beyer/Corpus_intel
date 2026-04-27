"""Topic modelling — sample-induce-classify two-pass.

Pipeline:
  1. User creates a `TopicSet` (name, scope slice, sample_size).
  2. `induce(...)` calls Sonnet once on a random sample (typically 200 rows),
     gets back 4–8 topics with names, descriptions, keywords, and 3 example
     row indices each. Stored on `TopicSet.topics`.
  3. `classify(...)` calls Sonnet in batches over ALL in-scope rows,
     assigning each row to one topic_id (or "other"). Stored on
     `TopicSet.row_assignments` (row_idx → topic_id).
  4. User can rename a topic, merge topics, or delete the whole set.

We don't cache topic classifications on disk (unlike Phase 5). Topic induction
is inherently one-shot per set; rerunning classification is rare and the user
should see fresh results.

Both `induce` and `classify` are generators yielding SSE-friendly events.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import random
import re
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from corpus_intel.ai.claude_client import (
    ClaudeClientError,
    calc_cost,
    call_claude_cached,
    extract_json,
    MODEL_PRICING,
)
from corpus_intel.ai.prompts import (
    TOPIC_CLASSIFICATION_BATCH_TEMPLATE,
    TOPIC_CLASSIFICATION_SYSTEM_PROMPT,
    TOPIC_CLASSIFICATION_TOPICS_TEMPLATE,
    TOPIC_INDUCTION_SYSTEM_PROMPT,
    TOPIC_INDUCTION_USER_TEMPLATE,
)
from corpus_intel.app_state import AppState, ProvenanceEvent, TopicSet
from corpus_intel.constants import AI_MODELS

log = logging.getLogger("corpus_intel.topics")

DEFAULT_SAMPLE_SIZE = 200
DEFAULT_CLASSIFY_BATCH = 30
CHARS_PER_TOKEN = 3.5
OUTPUT_TOKENS_PER_ROW = 20
OTHER_TOPIC_ID = "other"
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


class TopicError(ValueError):
    """Raised when a topic operation can't proceed."""


# ─── Factory + mutations ────────────────────────────────────────────────────
def new_topic_set(
    name: str,
    *,
    scope_slice_id: str = "",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    target_k: int = 8,
    goal: str = "",
) -> TopicSet:
    name = (name or "").strip() or "Untitled topic set"
    return TopicSet(
        topic_set_id=f"ts_{uuid.uuid4().hex[:10]}",
        name=name,
        created_at=dt.datetime.now().isoformat(timespec="seconds"),
        sample_size=max(20, min(500, int(sample_size or DEFAULT_SAMPLE_SIZE))),
        target_k=max(3, min(20, int(target_k or 8))),
        scope_slice_id=scope_slice_id or "",
        goal=(goal or "").strip(),
        status="draft",
    )


def _slug(s: str) -> str:
    out = _SLUG_RE.sub("_", (s or "").lower()).strip("_")
    return out or "topic"


def _normalise_topics(raw: Any) -> List[Dict[str, Any]]:
    """Clean up topic dicts coming back from the model and ensure unique ids."""
    if not isinstance(raw, list):
        return []
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            continue
        tid = _slug(str(t.get("topic_id") or t.get("name") or f"topic_{i + 1}"))
        base = tid
        n = 2
        while tid in seen or tid == OTHER_TOPIC_ID:
            tid = f"{base}_{n}"
            n += 1
        seen.add(tid)
        name = str(t.get("name") or tid).strip()[:60] or tid
        desc = str(t.get("description") or "").strip()[:400]
        kws = t.get("keywords") or []
        if not isinstance(kws, list):
            kws = []
        kws = [str(k).strip()[:40] for k in kws if str(k).strip()][:8]
        examples = t.get("example_row_indices") or t.get("examples") or []
        if not isinstance(examples, list):
            examples = []
        examples_clean: List[int] = []
        for v in examples:
            try:
                examples_clean.append(int(v))
            except (TypeError, ValueError):
                continue
        out.append({
            "topic_id": tid,
            "name": name,
            "description": desc,
            "keywords": kws,
            "example_row_indices": examples_clean[:6],
            "count": 0,
        })
    return out


def format_topics_block(topics: List[Dict[str, Any]]) -> str:
    lines = []
    for t in topics:
        kws = ", ".join(t.get("keywords") or []) or "(no keywords)"
        lines.append(
            f"- id={t['topic_id']} — {t.get('name', t['topic_id'])}\n"
            f"  description: {(t.get('description') or '').strip() or '(none)'}\n"
            f"  keywords: {kws}"
        )
    return "\n".join(lines) if lines else "(no topics)"


# ─── Preflight ──────────────────────────────────────────────────────────────
def preflight(
    *,
    sample_size: int,
    classify_rows: int,
    batch_size: int = DEFAULT_CLASSIFY_BATCH,
    avg_chars: int = 300,
    induce_model: Optional[str] = None,
    classify_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Rough cost estimate for one induction + one full classification pass.

    Induction uses Sonnet (nuanced theme proposal). Classification uses Haiku
    (per-row "which of these N topics" — same tier as AI Coding).
    """
    induce_model   = induce_model   or AI_MODELS.get("topic_induce",   "claude-sonnet-4-6")
    classify_model = classify_model or AI_MODELS.get("topic_classify", "claude-haiku-4-5-20251001")
    p_ind = MODEL_PRICING.get(induce_model, {})
    p_cls = MODEL_PRICING.get(classify_model, {})

    induce_in  = math.ceil((sample_size * (avg_chars + 40) + 1500) / CHARS_PER_TOKEN)
    induce_out = math.ceil(sample_size * 6 + 300)  # small JSON per topic + rationale

    batches = math.ceil(classify_rows / batch_size) if classify_rows else 0
    classify_in  = math.ceil((classify_rows * (avg_chars + 20) + batches * 800) / CHARS_PER_TOKEN)
    classify_out = classify_rows * OUTPUT_TOKENS_PER_ROW

    induce_cost = 0.0
    classify_cost = 0.0
    if p_ind:
        induce_cost += induce_in  * p_ind["input"]  / 1_000_000
        induce_cost += induce_out * p_ind["output"] / 1_000_000
    if p_cls:
        classify_cost += classify_in  * p_cls["input"]  / 1_000_000
        classify_cost += classify_out * p_cls["output"] / 1_000_000

    # Induction: ~20s on Sonnet. Classification: Haiku batches are ~2s each.
    induce_seconds = 20.0
    classify_seconds = 2.5 * batches

    return {
        "sample_size": sample_size,
        "classify_rows": classify_rows,
        "batches": batches,
        "batch_size": batch_size,
        "estimated_input_tokens": int(induce_in + classify_in),
        "estimated_output_tokens": int(induce_out + classify_out),
        # Split estimate so the UI can show "induce now" vs "induce + classify"
        "estimated_cost_induce_usd":   round(induce_cost, 4),
        "estimated_cost_classify_usd": round(classify_cost, 4),
        "estimated_cost_usd":          round(induce_cost + classify_cost, 4),
        "estimated_seconds_induce":   round(induce_seconds, 1),
        "estimated_seconds_classify": round(classify_seconds, 1),
        "estimated_seconds":          round(induce_seconds + classify_seconds, 1),
        "induce_model": induce_model,
        "classify_model": classify_model,
        "model": classify_model,  # kept for backward-compat with the UI's single "Model" cell
    }


# ─── Induction ──────────────────────────────────────────────────────────────
def induce(
    state: AppState,
    ts: TopicSet,
    sample_rows: List[Tuple[int, str]],
    *,
    api_key: str,
    model: Optional[str] = None,
    goal: str = "",
) -> Iterator[Dict[str, Any]]:
    """Single Sonnet call to propose topics from `sample_rows`.

    Events: start, error, done.
    """
    model = model or AI_MODELS.get("topic_induce", "claude-sonnet-4-6")
    goal = (goal or ts.goal or "").strip()

    yield {"type": "start", "sample_rows": len(sample_rows), "model": model}

    ts.status = "inducing"
    ts.last_error = ""

    sample_lines = []
    for (ri, text) in sample_rows[:500]:
        snippet = (text or "").strip().replace("\n", " ")[:400]
        sample_lines.append(f"[{ri}] {snippet}")
    user_tail = TOPIC_INDUCTION_USER_TEMPLATE.format(
        goal=goal or "(none — just induce the most distinctive themes)",
        n=len(sample_lines),
        sample="\n".join(sample_lines),
        target_k=int(getattr(ts, "target_k", 8) or 8),
    )

    t0 = time.time()
    try:
        text, usage, used_model = call_claude_cached(
            system=TOPIC_INDUCTION_SYSTEM_PROMPT,
            user_cache_block="(topic induction — one-shot call, no shared context)",
            user_tail=user_tail,
            task="topic_induce",
            api_key=api_key,
            max_tokens=3000,
        )
    except ClaudeClientError as e:
        ts.status = "error"
        ts.last_error = str(e)
        yield {"type": "error", "message": str(e)}
        return

    payload = extract_json(text) or {}
    raw_topics = payload.get("topics") if isinstance(payload, dict) else None
    topics = _normalise_topics(raw_topics)
    if not topics:
        ts.status = "error"
        ts.last_error = "Model did not return any topics."
        yield {"type": "error", "message": ts.last_error}
        return

    cost = round(calc_cost(usage, used_model), 5)
    ts.topics = topics
    ts.row_assignments = {}   # fresh induction invalidates any prior assignments
    ts.total_rows = 0
    ts.model = used_model
    ts.cost_usd = round((ts.cost_usd or 0.0) + cost, 5)
    ts.goal = goal
    ts.status = "induced"

    state.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="topics_induce",
            params={
                "topic_set_id": ts.topic_set_id,
                "sample_rows": len(sample_rows),
                "topics": len(topics),
                "cost_usd": cost,
                "model": used_model,
            },
        )
    )

    yield {
        "type": "done",
        "topics": len(topics),
        "cost_usd": cost,
        "seconds": round(time.time() - t0, 2),
        "usage": usage,
        "rationale": (payload.get("rationale") or "") if isinstance(payload, dict) else "",
    }


# ─── Classification ─────────────────────────────────────────────────────────
def _parse_rows(text: str) -> List[Dict[str, Any]]:
    payload = extract_json(text)
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    if isinstance(payload, list):
        return payload
    return []


def classify(
    state: AppState,
    ts: TopicSet,
    rows: List[Tuple[int, str]],
    *,
    api_key: str,
    batch_size: int = DEFAULT_CLASSIFY_BATCH,
    model: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Classify `rows` into the existing topics. Updates ts.row_assignments in place.

    Events: start, batch, error, done.
    """
    if not ts.topics:
        yield {"type": "error", "message": "This topic set has no topics yet. Run induction first."}
        return

    model = model or AI_MODELS.get("topic_classify", "claude-haiku-4-5-20251001")
    topic_ids = {t["topic_id"] for t in ts.topics}
    batches = math.ceil(len(rows) / batch_size) if rows else 0

    yield {
        "type": "start",
        "rows_total": len(rows),
        "batches": batches,
        "batch_size": batch_size,
        "model": model,
    }

    ts.status = "classifying"
    ts.last_error = ""
    ts.row_assignments = {}   # wipe prior — this is a full reclassify

    topics_block = format_topics_block(ts.topics)
    topics_prefix = TOPIC_CLASSIFICATION_TOPICS_TEMPLATE.format(topics_block=topics_block)

    total_cost = 0.0
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
    applied_total = 0

    for b_idx in range(0, len(rows), batch_size):
        batch = rows[b_idx:b_idx + batch_size]
        rows_json = json.dumps(
            [{"row_idx": ri, "text": (t or "")[:1200]} for ri, t in batch],
            ensure_ascii=False,
        )
        user_tail = TOPIC_CLASSIFICATION_BATCH_TEMPLATE.format(rows_json=rows_json)

        t0 = time.time()
        try:
            text, usage, used_model = call_claude_cached(
                system=TOPIC_CLASSIFICATION_SYSTEM_PROMPT,
                user_cache_block=topics_prefix,
                user_tail=user_tail,
                task="topic_classify",
                api_key=api_key,
                max_tokens=min(4096, 120 + len(batch) * OUTPUT_TOKENS_PER_ROW * 2),
            )
        except ClaudeClientError as e:
            ts.status = "error"
            ts.last_error = str(e)
            yield {"type": "error", "message": str(e), "batch_idx": b_idx // batch_size + 1}
            return

        batch_cost = calc_cost(usage, used_model)
        total_cost += batch_cost
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)

        answers = _parse_rows(text)
        answer_by_row: Dict[int, Dict[str, Any]] = {}
        for a in answers:
            try:
                ri = int(a.get("row_idx"))
            except (TypeError, ValueError):
                continue
            answer_by_row[ri] = a

        applied_in_batch = 0
        for (ri, _t) in batch:
            a = answer_by_row.get(ri)
            if a is None:
                continue
            tid = str(a.get("topic_id") or "").strip()
            if tid != OTHER_TOPIC_ID and tid not in topic_ids:
                tid = OTHER_TOPIC_ID
            ts.row_assignments[str(ri)] = tid
            applied_in_batch += 1
        applied_total += applied_in_batch

        yield {
            "type": "batch",
            "batch_idx": (b_idx // batch_size) + 1,
            "rows_in_batch": len(batch),
            "applied": applied_in_batch,
            "missing_in_response": len(batch) - applied_in_batch,
            "cost_usd": round(batch_cost, 5),
            "seconds": round(time.time() - t0, 2),
            "cumulative_cost_usd": round(total_cost, 5),
            "applied_total": applied_total,
        }

    # Recompute per-topic counts.
    counts: Dict[str, int] = {t["topic_id"]: 0 for t in ts.topics}
    counts[OTHER_TOPIC_ID] = 0
    for tid in ts.row_assignments.values():
        counts[tid] = counts.get(tid, 0) + 1
    for t in ts.topics:
        t["count"] = int(counts.get(t["topic_id"], 0))

    ts.total_rows = len(rows)
    ts.cost_usd = round((ts.cost_usd or 0.0) + total_cost, 5)
    ts.model = model
    ts.status = "ready"

    state.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="topics_classify",
            params={
                "topic_set_id": ts.topic_set_id,
                "rows_classified": applied_total,
                "rows_total": len(rows),
                "cost_usd": round(total_cost, 5),
                "model": model,
            },
        )
    )

    yield {
        "type": "done",
        "applied_total": applied_total,
        "cost_usd": round(total_cost, 5),
        "other_count": int(counts.get(OTHER_TOPIC_ID, 0)),
        "usage": total_usage,
        "model": model,
    }


# ─── Mutations ──────────────────────────────────────────────────────────────
def rename_topic(ts: TopicSet, topic_id: str, new_name: str) -> Dict[str, Any]:
    new_name = (new_name or "").strip()
    if not new_name:
        raise TopicError("Topic name cannot be empty.")
    for t in ts.topics:
        if t["topic_id"] == topic_id:
            t["name"] = new_name[:60]
            return t
    raise TopicError(f"No topic with id {topic_id!r}.")


def update_topic(
    ts: TopicSet,
    topic_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    for t in ts.topics:
        if t["topic_id"] == topic_id:
            if name is not None:
                n = name.strip()
                if not n:
                    raise TopicError("Topic name cannot be empty.")
                t["name"] = n[:60]
            if description is not None:
                t["description"] = (description or "").strip()[:400]
            if keywords is not None:
                kws = [str(k).strip()[:40] for k in (keywords or []) if str(k).strip()][:8]
                t["keywords"] = kws
            return t
    raise TopicError(f"No topic with id {topic_id!r}.")


def merge_topics(ts: TopicSet, source_ids: List[str], target_id: str) -> Dict[str, Any]:
    """Merge every source topic into target. Row assignments are rewritten."""
    target = next((t for t in ts.topics if t["topic_id"] == target_id), None)
    if target is None:
        raise TopicError(f"No target topic {target_id!r}.")
    src_set = set(s for s in source_ids if s and s != target_id)
    if not src_set:
        raise TopicError("No source topics to merge.")
    # Reassign rows.
    moved = 0
    for k, v in ts.row_assignments.items():
        if v in src_set:
            ts.row_assignments[k] = target_id
            moved += 1
    # Remove the merged topics.
    ts.topics = [t for t in ts.topics if t["topic_id"] not in src_set]
    # Recount.
    counts: Dict[str, int] = {t["topic_id"]: 0 for t in ts.topics}
    counts[OTHER_TOPIC_ID] = 0
    for tid in ts.row_assignments.values():
        counts[tid] = counts.get(tid, 0) + 1
    for t in ts.topics:
        t["count"] = int(counts.get(t["topic_id"], 0))
    return {"target_id": target_id, "moved": moved, "removed": list(src_set)}


def sample_rows_for_topic(
    ts: TopicSet,
    topic_id: str,
    all_rows: Dict[int, str],
    *,
    n: int = 5,
) -> List[Tuple[int, str]]:
    """Return up to n (row_idx, text) pairs assigned to a topic_id."""
    ids = [int(k) for k, v in ts.row_assignments.items() if v == topic_id]
    if not ids:
        # Fall back to example_row_indices set during induction.
        for t in ts.topics:
            if t["topic_id"] == topic_id:
                ids = list(t.get("example_row_indices") or [])
                break
    random.Random(42).shuffle(ids)
    out: List[Tuple[int, str]] = []
    for ri in ids[: n * 3]:
        txt = all_rows.get(ri)
        if txt is None:
            continue
        out.append((ri, txt))
        if len(out) >= n:
            break
    return out
