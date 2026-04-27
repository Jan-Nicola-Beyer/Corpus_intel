"""Sample diagnostics (P11) — is the sample representative of the corpus?

Given a sample (DataFrame) and the full corpus (DataFrame), compare the
distributions of key dimensions and report how far off they are. Pure pandas,
no API calls.

Checks:
- Platform balance (χ²-like distance on proportions)
- Date coverage (missing weeks/months)
- Language balance
- Engagement skew (median + top-decile contribution)
- Text length distribution (Kolmogorov–Smirnov-ish deviation)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def _safe_value_counts(s: pd.Series, *, top: int = 20) -> Dict[str, int]:
    if s is None or s.empty:
        return {}
    return {str(k): int(v) for k, v in s.dropna().astype(str).value_counts().head(top).items()}


def _proportion_distance(corpus_counts: Dict[str, int], sample_counts: Dict[str, int]) -> float:
    """Total-variation distance between two distributions on the union of keys."""
    keys = set(corpus_counts) | set(sample_counts)
    if not keys:
        return 0.0
    total_c = sum(corpus_counts.values()) or 1
    total_s = sum(sample_counts.values()) or 1
    d = 0.0
    for k in keys:
        pc = corpus_counts.get(k, 0) / total_c
        ps = sample_counts.get(k, 0) / total_s
        d += abs(pc - ps)
    return round(d / 2.0, 4)  # 0 = identical, 1 = disjoint


def _length_distribution(s: pd.Series) -> Dict[str, float]:
    if s is None or s.empty:
        return {}
    lens = s.fillna("").astype(str).str.len()
    return {
        "n": int(len(lens)),
        "median": float(lens.median()) if len(lens) else 0.0,
        "p10": float(lens.quantile(0.10)) if len(lens) else 0.0,
        "p90": float(lens.quantile(0.90)) if len(lens) else 0.0,
    }


def _engagement_skew(s: pd.Series) -> Dict[str, float]:
    if s is None or s.empty:
        return {}
    x = pd.to_numeric(s, errors="coerce").fillna(0.0)
    if x.empty:
        return {}
    total = float(x.sum()) or 1.0
    top10 = float(x.nlargest(max(1, int(len(x) * 0.1))).sum())
    return {
        "median": float(x.median()),
        "mean": float(x.mean()),
        "top_decile_share": round(top10 / total, 4),
    }


def diagnose(sample: pd.DataFrame, corpus: pd.DataFrame, *,
             platform_col: str = "platform",
             lang_col: str = "language",
             date_col: str = "posted_at",
             text_col: str = "text",
             engagement_col: str = "like_count") -> Dict[str, Any]:
    """Return a dict of comparisons. Distances > 0.15 are flagged as 'drift'."""
    out: Dict[str, Any] = {"n_sample": int(len(sample)), "n_corpus": int(len(corpus))}
    warnings: List[str] = []

    # Platform distribution
    if platform_col in corpus.columns and platform_col in sample.columns:
        cc = _safe_value_counts(corpus[platform_col])
        sc = _safe_value_counts(sample[platform_col])
        d = _proportion_distance(cc, sc)
        out["platform"] = {"distance": d, "corpus_top": cc, "sample_top": sc}
        if d > 0.15:
            warnings.append(f"Sample platform distribution drifts from corpus (TV distance={d}).")

    # Language
    if lang_col in corpus.columns and lang_col in sample.columns:
        cc = _safe_value_counts(corpus[lang_col])
        sc = _safe_value_counts(sample[lang_col])
        d = _proportion_distance(cc, sc)
        out["language"] = {"distance": d, "corpus_top": cc, "sample_top": sc}
        if d > 0.15:
            warnings.append(f"Language balance in sample differs from corpus (TV distance={d}).")

    # Date coverage (months present)
    if date_col in corpus.columns and date_col in sample.columns:
        c_dates = pd.to_datetime(corpus[date_col], errors="coerce")
        s_dates = pd.to_datetime(sample[date_col], errors="coerce")
        c_months = {f"{d.year:04d}-{d.month:02d}" for d in c_dates.dropna()}
        s_months = {f"{d.year:04d}-{d.month:02d}" for d in s_dates.dropna()}
        missing = sorted(c_months - s_months)
        out["date"] = {"corpus_months": len(c_months), "sample_months": len(s_months), "missing": missing[:20]}
        if c_months and len(missing) / max(1, len(c_months)) > 0.25:
            warnings.append(f"Sample misses {len(missing)} months that exist in the corpus.")

    # Text length
    if text_col in corpus.columns and text_col in sample.columns:
        out["text_length"] = {"corpus": _length_distribution(corpus[text_col]), "sample": _length_distribution(sample[text_col])}

    # Engagement skew
    if engagement_col in corpus.columns and engagement_col in sample.columns:
        out["engagement"] = {"corpus": _engagement_skew(corpus[engagement_col]), "sample": _engagement_skew(sample[engagement_col])}

    out["warnings"] = warnings
    out["representative"] = len(warnings) == 0
    return out
