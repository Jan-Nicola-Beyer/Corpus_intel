"""Merge selected datasets into a single canonical corpus.

Steps per dataset:
  1. Load the cached DataFrame from parquet.
  2. Apply its saved source→canonical mapping via core.sources._base.apply_mapping.
  3. Tag each row with `source_dataset` (original filename + dataset_id).
  4. Concat all datasets.
  5. Parse `created_at` to a proper datetime column (failures become NaT).
  6. Dedupe in two passes:
       a. Exact duplicate `post_id` (ignores blanks).
       b. Near-duplicate text — SHA1 of a normalized version of the text.
  7. Return the merged DataFrame plus a stats dict.

The output DataFrame has all 22 canonical columns plus:
  * `source_dataset`  — "<filename> · <dataset_id>"
  * `source_id`       — adapter id (brandwatch, twitter, ...)
  * `_row_idx`        — stable 0-based row index (also the DataFrame index)

Pure pandas. No AI. Safe to run on 100k-row inputs.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from corpus_intel.app_state import AppState, DataFrameMeta
from corpus_intel.core.sources._base import apply_mapping

log = logging.getLogger("corpus_intel.merge")

_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")
# Unicode-aware "keep letters, digits, and whitespace". The old pattern
# `[^0-9a-z\s]` kept only ASCII, so two Spanish or Japanese posts differing
# only in URL or punctuation would hash differently and escape near-dup
# dedupe. \w in Python 3 regex is Unicode by default (letters + digits + _).
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


@dataclass
class MergeStats:
    input_rows: int = 0
    datasets: int = 0
    exact_post_id_duplicates: int = 0
    near_text_duplicates: int = 0
    final_rows: int = 0
    per_dataset: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_rows": self.input_rows,
            "datasets": self.datasets,
            "exact_post_id_duplicates": self.exact_post_id_duplicates,
            "near_text_duplicates": self.near_text_duplicates,
            "final_rows": self.final_rows,
            "per_dataset": self.per_dataset,
        }


def _normalize_text_for_hash(value: Any) -> str:
    """Produce a stable, locale-robust fingerprint string for dedup hashing.

    * NFKC normalization folds compatibility forms (e.g. full-width digits,
      ligatures) so visually-equal strings hash the same.
    * `casefold()` handles Turkish dotted-I, German ß, etc. — `lower()` does
      not round-trip these correctly and used to let near-duplicate Turkish /
      German posts escape the dedup.
    * `\w` in the Unicode regex keeps letters + digits in every script, so
      non-Latin corpora are deduplicated too. The previous `[^0-9a-z\\s]`
      pattern stripped every non-ASCII letter, producing identical-after-strip
      strings for unrelated Japanese / Arabic posts.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = unicodedata.normalize("NFKC", str(value)).casefold()
    s = _URL_RE.sub(" ", s)
    s = _NON_WORD_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    # Trim very long inputs — stability and speed.
    return s[:600]


def _text_hash(value: Any) -> Optional[str]:
    norm = _normalize_text_for_hash(value)
    if len(norm) < 8:
        return None  # too short to treat as near-dup
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# DD/MM/YYYY and DD.MM.YYYY — the heuristic treats a value as "day-first" when
# its first two digits are > 12 (can only be a day). If at least 15% of the
# non-null values in a series look day-first, parse the whole series that way.
_DAYFIRST_SLASH_RE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-]\d{2,4}")


def _detect_dayfirst(series: pd.Series, min_fraction: float = 0.15) -> bool:
    """Heuristically detect DD/MM/YYYY vs MM/DD/YYYY from the data.

    Many European exports (Brandwatch in EU locale, German CSV tools) emit
    dates like 14/03/2026 which pandas would otherwise silently parse as
    November 4th when the day is ≤ 12. If any appreciable fraction of the
    values have a first field > 12, treat the column as day-first.
    ISO-formatted strings (2026-03-14T…) are unaffected — they don't match.
    """
    try:
        sample = series.dropna().astype(str).head(200)
    except Exception:
        return False
    if sample.empty:
        return False
    hits = 0
    total = 0
    for v in sample:
        m = _DAYFIRST_SLASH_RE.match(v)
        if not m:
            continue
        total += 1
        try:
            first = int(m.group(1))
            second = int(m.group(2))
        except ValueError:
            continue
        if first > 12 and second <= 12:
            hits += 1
    if total == 0:
        return False
    return (hits / total) >= min_fraction


def _parse_datetime(series: pd.Series) -> pd.Series:
    """Best-effort datetime parse (UTC). Unparseable values become NaT.

    Honours the dayfirst heuristic for European-style DD/MM/YYYY exports;
    ISO-8601 values still parse unchanged (pandas ignores dayfirst for
    unambiguous formats)."""
    dayfirst = False
    try:
        dayfirst = _detect_dayfirst(series)
    except Exception:
        dayfirst = False
    try:
        return pd.to_datetime(series, errors="coerce", utc=True, dayfirst=dayfirst)
    except Exception:
        return pd.to_datetime(pd.Series([None] * len(series)), errors="coerce", utc=True)


def build_corpus(
    state: AppState,
    dataset_ids: List[str],
    *,
    dedupe_near_text: bool = True,
) -> tuple[pd.DataFrame, MergeStats]:
    """Combine datasets into a single canonical-column DataFrame.

    Raises ValueError if no usable datasets were found.
    """
    stats = MergeStats(datasets=0)
    if not dataset_ids:
        raise ValueError("no datasets selected")

    frames: List[pd.DataFrame] = []

    for ds_id in dataset_ids:
        meta: Optional[DataFrameMeta] = state.datasets.get(ds_id)
        if not meta:
            log.warning("skip unknown dataset %s", ds_id)
            continue
        df = state.load_dataset(ds_id)
        if df is None or df.empty:
            log.warning("skip empty dataset %s", ds_id)
            continue

        mapping = meta.mapping or {}
        if not mapping:
            log.warning("dataset %s has no mapping; skipping", ds_id)
            continue

        canon = apply_mapping(df, mapping)
        # Tag rows so the analyst can trace each back to its source file.
        canon["source_dataset"] = f"{meta.original_filename} \u00b7 {ds_id}"
        canon["source_id"] = meta.source_id
        frames.append(canon)
        stats.datasets += 1
        stats.per_dataset.append({
            "dataset_id": ds_id,
            "filename": meta.original_filename,
            "rows_in": int(len(canon)),
            "source_id": meta.source_id,
        })

    if not frames:
        raise ValueError("no datasets produced usable rows (check mappings)")

    merged = pd.concat(frames, ignore_index=True)
    stats.input_rows = int(len(merged))

    # Normalize created_at to a tz-aware datetime.
    if "created_at" in merged.columns:
        merged["created_at"] = _parse_datetime(merged["created_at"])

    # ─── Dedupe pass 1: exact post_id matches ──────────────────────────────
    if "post_id" in merged.columns:
        pid = merged["post_id"].astype("string").str.strip()
        has_id = pid.notna() & (pid.str.len() > 0)
        dup_mask = has_id & pid.duplicated(keep="first")
        stats.exact_post_id_duplicates = int(dup_mask.sum())
        if stats.exact_post_id_duplicates:
            merged = merged.loc[~dup_mask].copy()

    # ─── Dedupe pass 2: near-dup by normalized text hash ───────────────────
    if dedupe_near_text and "text" in merged.columns:
        hashes = merged["text"].apply(_text_hash)
        dup_mask = hashes.notna() & hashes.duplicated(keep="first")
        stats.near_text_duplicates = int(dup_mask.sum())
        if stats.near_text_duplicates:
            merged = merged.loc[~dup_mask].copy()

    merged = merged.reset_index(drop=True)
    merged["_row_idx"] = merged.index.astype(int)
    stats.final_rows = int(len(merged))
    return merged, stats


def corpus_facets(df: pd.DataFrame, top_k: int = 15) -> Dict[str, Any]:
    """Cheap facets for UI chips — top values per faceted column."""
    def top_values(col: str) -> List[Dict[str, Any]]:
        if col not in df.columns:
            return []
        series = df[col].dropna()
        if series.empty:
            return []
        counts = series.astype("string").value_counts().head(top_k)
        return [{"value": str(k), "count": int(v)} for k, v in counts.items()]

    date_min = date_max = None
    if "created_at" in df.columns and not df["created_at"].isna().all():
        try:
            date_min = df["created_at"].min().isoformat()
            date_max = df["created_at"].max().isoformat()
        except Exception:
            date_min = date_max = None

    return {
        "platforms":       top_values("platform"),
        "languages":       top_values("language"),
        "source_datasets": top_values("source_dataset"),
        "source_ids":      top_values("source_id"),
        "countries":       top_values("country"),
        "date_min":        date_min,
        "date_max":        date_max,
    }
