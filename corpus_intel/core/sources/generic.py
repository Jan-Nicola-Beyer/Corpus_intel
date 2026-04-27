"""Generic fallback — no adapter-wired mapping. Relies on schema fuzzy matching."""
from __future__ import annotations

import pandas as pd

from corpus_intel.core.schema import suggest_mapping

from ._base import apply_mapping

SOURCE_ID = "generic"
SOURCE_NAME = "Generic CSV / XLSX"
CANONICAL_COLUMNS: dict = {}


def detect(df: pd.DataFrame, filename: str) -> float:
    # Generic is a floor: always plausible, but a weak confidence so specific
    # adapters win on equal footing.
    return 0.15


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    suggestions = suggest_mapping(list(df.columns))
    mapping = {
        src: info["canonical"]
        for src, info in suggestions.items()
        if info["canonical"]
    }
    return apply_mapping(df, mapping)
