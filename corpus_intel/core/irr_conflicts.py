"""IRR conflict helpers (P11) — find rows where coders disagree, suggest resolution.

Given a coded dataset with multiple coders, return rows where any two coders
assigned different categories. Each conflict entry carries enough info for the
UI to render the row + disagreeing labels and to record an adjudication.

Storage for adjudications: sessions/irr_adjudications.jsonl
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from corpus_intel.constants import SESSION_DIR

log = logging.getLogger("corpus_intel.irr_conflicts")

_ADJ_PATH = os.path.join(SESSION_DIR, "irr_adjudications.jsonl")


def find_conflicts(
    df: pd.DataFrame,
    *,
    row_col: str = "row_hash",
    coder_col: str = "coder",
    cat_col: str = "category",
) -> List[Dict[str, Any]]:
    """Return one entry per (row_hash) where at least 2 coders disagree."""
    if df is None or df.empty or row_col not in df.columns:
        return []
    if coder_col not in df.columns or cat_col not in df.columns:
        return []

    work = df[[row_col, coder_col, cat_col]].dropna()
    if work.empty:
        return []

    conflicts: List[Dict[str, Any]] = []
    for row_id, grp in work.groupby(row_col):
        cat_by_coder = grp.groupby(coder_col)[cat_col].agg(lambda xs: sorted(set(str(x) for x in xs)))
        if len(cat_by_coder) < 2:
            continue
        # conflict if any two coders have disjoint cat sets
        sets = [set(v) for v in cat_by_coder.values]
        if any(sets[i] != sets[j] for i in range(len(sets)) for j in range(i + 1, len(sets))):
            conflicts.append({
                "row_id": str(row_id),
                "coders": {str(c): list(v) for c, v in cat_by_coder.items()},
                "unique_cats": sorted({c for s in sets for c in s}),
            })
    return conflicts


def adjudicate(row_id: str, final_cat: str, *, adjudicator: str = "", note: str = "") -> Dict[str, Any]:
    """Persist an adjudication decision for one conflict."""
    entry = {
        "row_id": str(row_id),
        "final_cat": str(final_cat),
        "adjudicator": str(adjudicator),
        "note": str(note)[:500],
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(_ADJ_PATH), exist_ok=True)
    with open(_ADJ_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_adjudications(limit: int = 1000) -> List[Dict[str, Any]]:
    if not os.path.exists(_ADJ_PATH):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(_ADJ_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out[-limit:]


def summarize_conflicts(conflicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not conflicts:
        return {"count": 0, "cats_involved": [], "coders_involved": []}
    cats, coders = set(), set()
    for c in conflicts:
        cats.update(c.get("unique_cats") or [])
        coders.update((c.get("coders") or {}).keys())
    return {"count": len(conflicts), "cats_involved": sorted(cats), "coders_involved": sorted(coders)}
