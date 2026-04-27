"""UMAP / t-SNE scatter projection of the TF-IDF matrix (P12).

Projects the already-built semantic-search vectors to 2-D so the frontend can
scatter-plot them. Cached by snapshot_id.

Falls back gracefully:
1. umap-learn (best quality) → if `umap` not installed,
2. scikit-learn TruncatedSVD → PCA-style projection.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

import numpy as np

from corpus_intel.core import semantic_search as _semsearch

log = logging.getLogger("corpus_intel.umap")

_lock = threading.Lock()
_cache: Dict[str, List[Dict[str, Any]]] = {}


def project(snapshot_id: str, *, method: str = "auto", n_neighbors: int = 15,
            min_dist: float = 0.1, sample_size: int = 5000) -> List[Dict[str, Any]]:
    """Return a list of {row_id, x, y} — cached by snapshot_id."""
    cached = _cache.get(snapshot_id)
    if cached is not None:
        return cached

    idx = _semsearch.get_index(snapshot_id)
    if idx is None:
        raise RuntimeError("Build a semantic-search index first.")

    m = idx.matrix
    n = m.shape[0]
    take = min(n, sample_size)
    sel = np.random.default_rng(0).choice(n, size=take, replace=False) if n > take else np.arange(n)
    sub = m[sel]

    # Method preference: umap > truncated SVD.
    xy = None
    try:
        if method in ("auto", "umap"):
            import umap  # type: ignore
            reducer = umap.UMAP(n_components=2, n_neighbors=min(n_neighbors, take - 1 if take > 2 else 2),
                                 min_dist=min_dist, metric="cosine", random_state=0)
            xy = reducer.fit_transform(sub.toarray() if hasattr(sub, "toarray") else sub)
    except Exception:
        xy = None

    if xy is None:
        from sklearn.decomposition import TruncatedSVD  # type: ignore
        d = min(50, max(2, take - 1))
        svd = TruncatedSVD(n_components=d, random_state=0)
        compressed = svd.fit_transform(sub)
        # project compressed → 2D by picking top-2 singular components
        xy = compressed[:, :2]

    pts: List[Dict[str, Any]] = []
    for i, j in enumerate(sel):
        pts.append({
            "row_id": idx.row_ids[j],
            "x": float(xy[i, 0]),
            "y": float(xy[i, 1]),
        })
    with _lock:
        _cache[snapshot_id] = pts
    return pts


def clear(snapshot_id: Optional[str] = None) -> None:
    with _lock:
        if snapshot_id is None:
            _cache.clear()
        else:
            _cache.pop(snapshot_id, None)
