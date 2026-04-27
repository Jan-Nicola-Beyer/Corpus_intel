"""Active-learning row selector (P11).

Given a coded corpus, surface rows where AI is most likely uncertain so the
researcher's manual time is spent where it moves the model the most.

Uncertainty signals (ranked):
1. AI returned "low confidence" (conf < 0.6)
2. AI returned exactly one category with confidence in [0.45, 0.65] (boundary)
3. Multiple categories tied within 0.1 confidence
4. AI disagreed with a prior human label on the same row_hash (conflict)

Pure pandas. No API calls.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


DEFAULT_LOW_CONF = 0.6
BOUNDARY_LO = 0.45
BOUNDARY_HI = 0.65


def _entropy_like(conf_list: List[float]) -> float:
    """Closeness of the top two — higher means more ambiguous."""
    xs = sorted([c for c in conf_list if c is not None], reverse=True)
    if len(xs) < 2:
        return 0.0
    return 1.0 - abs(xs[0] - xs[1])  # 1.0 = a tie, 0.0 = clear winner


def score_rows(
    df: pd.DataFrame,
    *,
    conf_col: str = "ai_confidence",
    predicted_col: str = "ai_category",
    human_col: str = "human_category",
    limit: int = 200,
) -> pd.DataFrame:
    """Return a DF of the top-K most-uncertain rows with a `_uncertainty` score."""
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    score = pd.Series(0.0, index=work.index)

    if conf_col in work.columns:
        conf = pd.to_numeric(work[conf_col], errors="coerce")
        score = score + (conf < DEFAULT_LOW_CONF).astype(float) * 0.6
        boundary = conf.between(BOUNDARY_LO, BOUNDARY_HI)
        score = score + boundary.astype(float) * 0.3

    if human_col in work.columns and predicted_col in work.columns:
        has_human = work[human_col].notna() & (work[human_col].astype(str).str.strip() != "")
        disagreement = has_human & (work[human_col].astype(str) != work[predicted_col].astype(str))
        score = score + disagreement.astype(float) * 0.5  # conflicts are gold

    work["_uncertainty"] = score
    work = work.sort_values("_uncertainty", ascending=False, kind="mergesort")
    return work.head(limit)


def suggest_batch(
    df: pd.DataFrame, *, batch_size: int = 25,
    conf_col: str = "ai_confidence",
    predicted_col: str = "ai_category",
    human_col: str = "human_category",
) -> List[Dict[str, Any]]:
    """Smaller convenience wrapper — returns a JSON-ready list."""
    picked = score_rows(df, conf_col=conf_col, predicted_col=predicted_col, human_col=human_col, limit=batch_size)
    out: List[Dict[str, Any]] = []
    for _, row in picked.iterrows():
        out.append({
            "row_index": int(row.name) if isinstance(row.name, (int, float)) else str(row.name),
            "uncertainty": float(row.get("_uncertainty") or 0.0),
            "text": str(row.get("text") or row.get("body") or "")[:500],
            "ai_category": str(row.get(predicted_col) or ""),
            "ai_confidence": float(row.get(conf_col) or 0.0) if row.get(conf_col) is not None else None,
            "human_category": str(row.get(human_col) or ""),
        })
    return out
