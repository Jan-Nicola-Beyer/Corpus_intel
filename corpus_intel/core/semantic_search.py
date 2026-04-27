"""Semantic-ish search (P12) via TF-IDF + cosine similarity.

No embeddings API needed (Anthropic doesn't offer one) — scikit-learn's
TfidfVectorizer gives us character-n-gram + word term vectors that are a
respectable approximation for exploratory search over social-media text.

The matrix is built once per snapshot and cached by snapshot_id; subsequent
queries are fast (a single sparse dot-product).

Pure Python. No local LLMs.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("corpus_intel.semsearch")

_cache_lock = threading.Lock()
_cache: Dict[str, "Index"] = {}


@dataclass
class Index:
    snapshot_id: str
    vectorizer: Any
    matrix: Any
    row_ids: List[str]


def _get_sklearn():
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore
        return TfidfVectorizer, cosine_similarity
    except ImportError as e:
        raise RuntimeError(
            "semantic_search needs scikit-learn. Install with `pip install scikit-learn`."
        ) from e


def build_index(snapshot_id: str, df: pd.DataFrame, *, text_col: str = "text",
                row_id_col: str = "row_hash") -> Index:
    TfidfVectorizer, _ = _get_sklearn()
    if text_col not in df.columns:
        raise ValueError(f"column '{text_col}' not in dataframe")
    texts = df[text_col].fillna("").astype(str).tolist()
    row_ids = (df[row_id_col].astype(str).tolist() if row_id_col in df.columns
               else [str(i) for i in df.index])

    vec = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.9,
        sublinear_tf=True,
        lowercase=True,
        max_features=100_000,
    )
    matrix = vec.fit_transform(texts)
    idx = Index(snapshot_id=snapshot_id, vectorizer=vec, matrix=matrix, row_ids=row_ids)
    with _cache_lock:
        _cache[snapshot_id] = idx
    return idx


def get_index(snapshot_id: str) -> Optional[Index]:
    return _cache.get(snapshot_id)


def search(snapshot_id: str, query: str, *, k: int = 20) -> List[Dict[str, Any]]:
    idx = _cache.get(snapshot_id)
    if idx is None:
        raise RuntimeError("Index not built — call build_index() first.")
    _, cosine_similarity = _get_sklearn()
    q_vec = idx.vectorizer.transform([query or ""])
    sims = cosine_similarity(q_vec, idx.matrix).ravel()
    if not len(sims):
        return []
    top = np.argsort(-sims)[: max(1, int(k))]
    out: List[Dict[str, Any]] = []
    for i in top:
        score = float(sims[i])
        if score <= 0:
            continue
        out.append({
            "row_id": idx.row_ids[i],
            "score": round(score, 6),
            "rank": len(out) + 1,
        })
    return out


def clear_index(snapshot_id: Optional[str] = None) -> None:
    with _cache_lock:
        if snapshot_id is None:
            _cache.clear()
        else:
            _cache.pop(snapshot_id, None)
