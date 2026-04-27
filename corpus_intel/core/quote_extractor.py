"""Quote extractor (P13) — pull illustrative quotes for each topic / category.

Given a labelled corpus, find N most representative posts per label. Uses
either (a) highest AI confidence, or (b) TF-IDF relevance to the label's
keywords.

Pure Python when scikit-learn is available; falls back to random sample
otherwise.
"""
from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("corpus_intel.quotes")


def extract_by_confidence(df: pd.DataFrame, *, label_col: str,
                          conf_col: str = "ai_confidence",
                          text_col: str = "text",
                          per_label: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """Pick the top-K most confident posts per label value."""
    if df is None or df.empty or label_col not in df.columns:
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for label, grp in df.dropna(subset=[label_col]).groupby(label_col):
        if conf_col in grp.columns:
            sorted_grp = grp.sort_values(conf_col, ascending=False)
        else:
            sorted_grp = grp
        picks = sorted_grp.head(per_label)
        out[str(label)] = [{
            "row_id": str(r.get("row_hash") or idx),
            "text": str(r.get(text_col) or "")[:500],
            "confidence": float(r.get(conf_col) or 0.0) if conf_col in r else None,
            "posted_at": str(r.get("created_at") or ""),
            "author": str(r.get("author_handle") or ""),
        } for idx, r in picks.iterrows()]
    return out


def extract_by_keywords(df: pd.DataFrame, keywords_per_label: Dict[str, List[str]],
                        *, text_col: str = "text", per_label: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """Rank posts within each label by cosine similarity to a label-specific keyword 'query'."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore
    except ImportError:
        log.warning("scikit-learn not installed — falling back to random sample")
        return extract_random(df, per_label=per_label)

    texts = df[text_col].fillna("").astype(str).tolist()
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_df=0.9, sublinear_tf=True, max_features=50_000)
    matrix = vec.fit_transform(texts)

    out: Dict[str, List[Dict[str, Any]]] = {}
    for label, kws in keywords_per_label.items():
        if not kws:
            continue
        q_vec = vec.transform([" ".join(kws)])
        sims = cosine_similarity(q_vec, matrix).ravel()
        top = np.argsort(-sims)[: per_label]
        out[str(label)] = []
        for i in top:
            out[str(label)].append({
                "row_id": str(df.iloc[i].get("row_hash") or int(i)),
                "text": str(df.iloc[i].get(text_col) or "")[:500],
                "score": round(float(sims[i]), 4),
                "posted_at": str(df.iloc[i].get("created_at") or ""),
                "author": str(df.iloc[i].get("author_handle") or ""),
            })
    return out


def extract_random(df: pd.DataFrame, *, text_col: str = "text", per_label: int = 5,
                   label_col: Optional[str] = None, seed: int = 0) -> Dict[str, List[Dict[str, Any]]]:
    random.seed(seed)
    if df is None or df.empty:
        return {}
    if label_col and label_col in df.columns:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for label, grp in df.groupby(label_col):
            sample = grp.sample(min(per_label, len(grp)), random_state=seed)
            out[str(label)] = [{"row_id": str(r.get("row_hash") or idx), "text": str(r.get(text_col) or "")[:500]}
                               for idx, r in sample.iterrows()]
        return out
    return {"_all": [{"row_id": str(r.get("row_hash") or idx), "text": str(r.get(text_col) or "")[:500]}
                    for idx, r in df.sample(min(per_label, len(df)), random_state=seed).iterrows()]}
