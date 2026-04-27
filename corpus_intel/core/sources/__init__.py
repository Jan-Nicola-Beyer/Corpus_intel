"""Registry for source adapters. Import each module so detection can iterate."""
from __future__ import annotations

from types import ModuleType
from typing import Dict, List, Optional

import pandas as pd

from . import (
    brandwatch,
    generic,
    mastodon,
    meta,
    news,
    reddit,
    tiktok,
    twitter,
    youtube,
)

# Order is stable; generic sits last so it's the tie-breaker.
ADAPTERS: List[ModuleType] = [
    brandwatch,
    twitter,
    meta,
    tiktok,
    youtube,
    reddit,
    mastodon,
    news,
    generic,
]

_BY_ID: Dict[str, ModuleType] = {a.SOURCE_ID: a for a in ADAPTERS}


def get_adapter(source_id: str) -> Optional[ModuleType]:
    return _BY_ID.get(source_id)


def list_adapters() -> List[Dict[str, str]]:
    return [{"id": a.SOURCE_ID, "name": a.SOURCE_NAME} for a in ADAPTERS]


def detect_all(df: pd.DataFrame, filename: str) -> List[Dict[str, object]]:
    """Run every adapter's detect() and return a ranked list of candidates.

    [{"id": "brandwatch", "name": "Brandwatch", "confidence": 0.95}, ...]
    """
    candidates = []
    for adapter in ADAPTERS:
        try:
            score = float(adapter.detect(df, filename))
        except Exception:
            score = 0.0
        candidates.append(
            {"id": adapter.SOURCE_ID, "name": adapter.SOURCE_NAME, "confidence": round(max(0.0, min(1.0, score)), 3)}
        )
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates
