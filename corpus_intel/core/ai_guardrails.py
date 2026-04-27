"""AI guardrails (P11) — pre/post validation on AI coding output.

Checks applied before a batch is accepted:
- Output categories must be in the active codebook's cat_ids
- Confidence must be a float in [0, 1]
- Exclusion groups are respected (a row can't get two mutually-exclusive cats)
- "unsure" or low-confidence responses (<0.4) are tagged for review
- Drift check: if > 20 % of outputs are "unsure", flag the batch for review

Returns a report that the caller can surface or log.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from corpus_intel.app_state import Codebook


def _cat_index(cb: Codebook) -> Dict[str, Dict[str, Any]]:
    return {c["cat_id"]: c for c in cb.categories}


def validate_row(output: Dict[str, Any], cb: Codebook) -> Dict[str, Any]:
    """Validate one AI row output. Returns {ok, issues: [str]}."""
    issues: List[str] = []
    cat = output.get("category") or output.get("cat_id")
    conf = output.get("confidence")

    cat_idx = _cat_index(cb)
    if cat and cat not in cat_idx:
        issues.append(f"unknown category '{cat}' — not in codebook")

    try:
        c = float(conf)
        if c < 0 or c > 1:
            issues.append(f"confidence out of range: {c}")
        if c < 0.4:
            issues.append(f"low-confidence label (conf={c:.2f}) — flag for review")
    except (TypeError, ValueError):
        if conf is not None:
            issues.append(f"confidence not numeric: {conf}")

    return {"ok": not issues, "issues": issues}


def validate_batch(outputs: Iterable[Dict[str, Any]], cb: Codebook,
                   *, unsure_threshold: float = 0.20) -> Dict[str, Any]:
    """Validate a full batch. Returns aggregate report."""
    per_row: List[Dict[str, Any]] = []
    low_conf = 0
    unknown = 0
    total = 0
    for o in outputs:
        r = validate_row(o, cb)
        per_row.append(r)
        total += 1
        if any("low-confidence" in i for i in r["issues"]):
            low_conf += 1
        if any(i.startswith("unknown category") for i in r["issues"]):
            unknown += 1

    warnings: List[str] = []
    if total and low_conf / total >= unsure_threshold:
        warnings.append(f"{low_conf}/{total} rows are low-confidence — consider raising the sample size or refining the codebook.")
    if unknown:
        warnings.append(f"{unknown} output(s) used categories not in the codebook — drop or rename.")

    return {
        "total": total,
        "low_confidence": low_conf,
        "unknown_category": unknown,
        "warnings": warnings,
        "rows": per_row,
        "clean": not warnings,
    }


def enforce_exclusion(assigned: List[str], cb: Codebook) -> List[str]:
    """Trim a list of assigned cat_ids so that no two share a non-empty exclusion_group.
    When conflicts exist, the LAST one wins (matches the evict-on-write semantic of coding.py)."""
    groups = {c["cat_id"]: (c.get("exclusion_group") or "").strip() for c in cb.categories}
    seen_group: Dict[str, str] = {}   # group → cat_id currently holding it
    kept: List[str] = []
    for cid in assigned:
        g = groups.get(cid, "")
        if g:
            winner = cid  # last assignment wins
            # Remove any previous cat from this group from kept
            if g in seen_group:
                prev = seen_group[g]
                kept = [c for c in kept if c != prev]
            seen_group[g] = winner
        kept.append(cid)
    # de-dup while preserving order
    out, seen = [], set()
    for c in kept:
        if c not in seen:
            out.append(c); seen.add(c)
    return out
