"""Plain-pandas filter engine for the merged corpus.

A FilterSpec mixes simple predicates. The Slicer (Phase 3) will add a full
boolean-query language; this module covers the everyday filters users need in
the Corpus view.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

log = logging.getLogger("corpus_intel.filters")


ENGAGEMENT_COLUMNS = ("like_count", "share_count", "comment_count", "view_count")


@dataclass
class FilterSpec:
    text: str = ""                    # search term (plain substring unless regex=True)
    regex: bool = False
    case_sensitive: bool = False
    platforms: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)
    source_datasets: List[str] = field(default_factory=list)
    countries: List[str] = field(default_factory=list)
    date_from: Optional[str] = None   # ISO-ish, inclusive
    date_to: Optional[str] = None     # ISO-ish, inclusive
    # Engagement thresholds (inclusive). None = no bound on that side.
    engagement: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FilterSpec":
        data = data or {}
        raw_eng = data.get("engagement") or {}
        engagement: Dict[str, Dict[str, Optional[float]]] = {}
        if isinstance(raw_eng, dict):
            for col, bounds in raw_eng.items():
                if col not in ENGAGEMENT_COLUMNS or not isinstance(bounds, dict):
                    continue
                lo = bounds.get("min")
                hi = bounds.get("max")
                clean: Dict[str, Optional[float]] = {}
                if lo is not None and str(lo).strip() != "":
                    try: clean["min"] = float(lo)
                    except (TypeError, ValueError): pass
                if hi is not None and str(hi).strip() != "":
                    try: clean["max"] = float(hi)
                    except (TypeError, ValueError): pass
                if clean:
                    engagement[col] = clean
        return cls(
            text=str(data.get("text", "") or ""),
            regex=bool(data.get("regex", False)),
            case_sensitive=bool(data.get("case_sensitive", False)),
            platforms=list(data.get("platforms") or []),
            languages=list(data.get("languages") or []),
            source_ids=list(data.get("source_ids") or []),
            source_datasets=list(data.get("source_datasets") or []),
            countries=list(data.get("countries") or []),
            date_from=data.get("date_from") or None,
            date_to=data.get("date_to") or None,
            engagement=engagement,
        )


def _in_list_mask(series: pd.Series, values: List[str]) -> pd.Series:
    if not values:
        return pd.Series(True, index=series.index)
    norm = series.astype("string").str.lower()
    wanted = {str(v).lower() for v in values if v is not None and str(v).strip()}
    if not wanted:
        return pd.Series(True, index=series.index)
    return norm.isin(wanted)


def _text_mask(series: pd.Series, spec: FilterSpec) -> pd.Series:
    if not spec.text:
        return pd.Series(True, index=series.index)
    text = series.astype("string").fillna("")
    if spec.regex:
        try:
            pattern = re.compile(spec.text, 0 if spec.case_sensitive else re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}")
        return text.str.contains(pattern.pattern, regex=True, case=spec.case_sensitive, na=False)
    return text.str.contains(spec.text, regex=False, case=spec.case_sensitive, na=False)


def _date_mask(series: pd.Series, spec: FilterSpec) -> pd.Series:
    if spec.date_from is None and spec.date_to is None:
        return pd.Series(True, index=series.index)
    if not pd.api.types.is_datetime64_any_dtype(series):
        # If the corpus doesn't have parsed dates, a date range can't match anything.
        return pd.Series(False, index=series.index)
    mask = pd.Series(True, index=series.index)
    if spec.date_from:
        try:
            lo = pd.to_datetime(spec.date_from, utc=True, errors="raise")
            mask &= series.notna() & (series >= lo)
        except Exception as e:
            raise ValueError(f"invalid date_from: {e}")
    if spec.date_to:
        try:
            hi = pd.to_datetime(spec.date_to, utc=True, errors="raise")
            # Treat 'YYYY-MM-DD' as end-of-day inclusive.
            if len(str(spec.date_to)) == 10:
                hi = hi + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
            mask &= series.notna() & (series <= hi)
        except Exception as e:
            raise ValueError(f"invalid date_to: {e}")
    return mask


def _engagement_mask(df: pd.DataFrame, spec: FilterSpec) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, bounds in (spec.engagement or {}).items():
        if col not in df.columns:
            # Column absent → a min bound can't match; a max-only bound matches everything.
            if bounds.get("min") is not None:
                mask &= False
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        lo = bounds.get("min")
        hi = bounds.get("max")
        if lo is not None:
            mask &= numeric.notna() & (numeric >= lo)
        if hi is not None:
            mask &= numeric.notna() & (numeric <= hi)
    return mask


def apply_filter(df: pd.DataFrame, spec: FilterSpec) -> pd.DataFrame:
    """Return the subset of df that satisfies every predicate in spec."""
    if df is None or df.empty:
        return df

    mask = pd.Series(True, index=df.index)

    if "text" in df.columns:
        mask &= _text_mask(df["text"], spec)
    if "platform" in df.columns:
        mask &= _in_list_mask(df["platform"], spec.platforms)
    if "language" in df.columns:
        mask &= _in_list_mask(df["language"], spec.languages)
    if "source_id" in df.columns:
        mask &= _in_list_mask(df["source_id"], spec.source_ids)
    if "source_dataset" in df.columns:
        mask &= _in_list_mask(df["source_dataset"], spec.source_datasets)
    if "country" in df.columns:
        mask &= _in_list_mask(df["country"], spec.countries)
    if "created_at" in df.columns:
        mask &= _date_mask(df["created_at"], spec)
    if spec.engagement:
        mask &= _engagement_mask(df, spec)

    return df.loc[mask].copy()


def paginate(df: pd.DataFrame, page: int, page_size: int) -> pd.DataFrame:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    start = (page - 1) * page_size
    return df.iloc[start:start + page_size]
