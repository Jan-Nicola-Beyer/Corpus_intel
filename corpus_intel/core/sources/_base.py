"""Shared helpers for source adapters."""
from __future__ import annotations

from typing import Dict, Iterable, List

import pandas as pd

from corpus_intel.core.schema import CANONICAL_NAMES


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s) if ch.isalnum())


def column_match_score(columns: Iterable[str], fingerprint: Iterable[str]) -> float:
    """Fraction of `fingerprint` columns that appear in `columns` (normalized)."""
    cols_norm = {_norm(c) for c in columns}
    fp = [_norm(c) for c in fingerprint]
    if not fp:
        return 0.0
    hits = sum(1 for f in fp if f in cols_norm)
    return hits / len(fp)


def filename_hint(filename: str, tokens: Iterable[str]) -> float:
    """Return 0.0 / 0.05 / 0.1 based on whether known tokens appear in filename."""
    if not filename:
        return 0.0
    name = filename.lower()
    matches = sum(1 for t in tokens if t.lower() in name)
    if matches == 0:
        return 0.0
    return min(0.1, 0.05 * matches)


def apply_mapping(df: pd.DataFrame, mapping: Dict[str, str]) -> pd.DataFrame:
    """Rename columns per mapping (source_col → canonical_col), add missing canonical
    columns with NA, drop unmapped source columns, and reorder to canonical order.
    Duplicate canonical targets keep the first.
    """
    cols_lower = {c.lower(): c for c in df.columns}
    taken: List[str] = []
    renamed: Dict[str, str] = {}
    for src, canon in mapping.items():
        if canon not in CANONICAL_NAMES or canon in taken:
            continue
        real = cols_lower.get(src.lower())
        if real is None:
            continue
        renamed[real] = canon
        taken.append(canon)

    out = df.rename(columns=renamed)
    # Keep only canonical columns that exist
    keep = [c for c in CANONICAL_NAMES if c in out.columns]
    out = out[keep].copy()
    # Add missing canonical columns as NA
    for c in CANONICAL_NAMES:
        if c not in out.columns:
            out[c] = pd.NA
    return out[CANONICAL_NAMES]
