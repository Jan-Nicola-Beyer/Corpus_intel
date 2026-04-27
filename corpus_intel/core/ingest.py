"""File ingest — encoding, delimiter, header detection; quality flags."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import chardet
import pandas as pd

log = logging.getLogger("corpus_intel.ingest")

_CSV_DELIMITERS = [",", "\t", ";", "|"]
_SAMPLE_BYTES = 65536


@dataclass
class IngestResult:
    df: pd.DataFrame
    encoding: str
    delimiter: str
    ext: str
    notes: List[str]


def _detect_encoding(blob: bytes) -> str:
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        return "utf-16"
    if blob.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    guess = chardet.detect(blob[:_SAMPLE_BYTES]) or {}
    enc = (guess.get("encoding") or "utf-8").lower()
    # chardet commonly returns "ascii" for small UTF-8 files; prefer utf-8.
    if enc == "ascii":
        enc = "utf-8"
    return enc


def _detect_delimiter(text: str) -> str:
    sample = text[:_SAMPLE_BYTES]
    try:
        return csv.Sniffer().sniff(sample, _CSV_DELIMITERS).delimiter
    except csv.Error:
        # Fall back: whichever candidate occurs most on the first line.
        first_line = sample.split("\n", 1)[0]
        counts = {d: first_line.count(d) for d in _CSV_DELIMITERS}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def _read_csv_like(blob: bytes, ext: str) -> Tuple[pd.DataFrame, str, str, List[str]]:
    notes: List[str] = []
    encoding = _detect_encoding(blob)
    try:
        text = blob.decode(encoding, errors="replace")
    except LookupError:
        encoding = "utf-8"
        text = blob.decode(encoding, errors="replace")

    delim = _detect_delimiter(text) if ext != ".tsv" else "\t"

    try:
        df = pd.read_csv(io.StringIO(text), sep=delim, engine="python", on_bad_lines="skip")
    except Exception as e:
        notes.append(f"pandas_read_csv_failed: {e!s}")
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python", on_bad_lines="skip")

    # Drop columns with blank names
    df = df.loc[:, [c for c in df.columns if str(c).strip() != ""]]
    # Normalize column names to plain strings
    df.columns = [str(c).strip() for c in df.columns]
    return df, encoding, delim, notes


def _read_jsonl(blob: bytes) -> Tuple[pd.DataFrame, str, str, List[str]]:
    encoding = _detect_encoding(blob)
    text = blob.decode(encoding, errors="replace")
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    df = pd.json_normalize(records) if records else pd.DataFrame()
    return df, encoding, "\\n", []


def _read_json(blob: bytes) -> Tuple[pd.DataFrame, str, str, List[str]]:
    encoding = _detect_encoding(blob)
    text = blob.decode(encoding, errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Some exports ship as NDJSON with a .json extension.
        return _read_jsonl(blob)
    if isinstance(payload, list):
        df = pd.json_normalize(payload)
    elif isinstance(payload, dict):
        # Heuristic: find the first list-of-records value
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                df = pd.json_normalize(v)
                break
        else:
            df = pd.json_normalize([payload])
    else:
        df = pd.DataFrame()
    return df, encoding, "json", []


def _read_xlsx(blob: bytes) -> Tuple[pd.DataFrame, str, str, List[str]]:
    df = pd.read_excel(io.BytesIO(blob), engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    return df, "binary", "xlsx", []


def read_upload(filename: str, blob: bytes) -> IngestResult:
    """Read an uploaded file blob into a DataFrame. Raises on unsupported ext."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".xlsx", ".xls"}:
        df, enc, delim, notes = _read_xlsx(blob)
    elif ext == ".jsonl" or ext == ".ndjson":
        df, enc, delim, notes = _read_jsonl(blob)
    elif ext == ".json":
        df, enc, delim, notes = _read_json(blob)
    elif ext in {".csv", ".tsv", ".txt"}:
        df, enc, delim, notes = _read_csv_like(blob, ext)
    else:
        raise ValueError(f"Unsupported extension: {ext}")

    return IngestResult(df=df, encoding=enc, delimiter=delim, ext=ext, notes=notes)


# ─── Quality flags ───────────────────────────────────────────────────────────
def quality_flags(df: pd.DataFrame, mapping: Dict[str, str]) -> Dict[str, Any]:
    """Inspect canonical coverage given a mapping. Produces user-facing warnings."""
    flags: Dict[str, Any] = {}
    canonical_to_source: Dict[str, str] = {}
    for src, canon in mapping.items():
        if canon and canon not in canonical_to_source:
            canonical_to_source[canon] = src

    # Missing text
    text_src = canonical_to_source.get("text")
    if not text_src:
        flags["missing_text"] = True
    else:
        na = df[text_src].isna().sum() if text_src in df.columns else 0
        empty = (df[text_src].astype(str).str.strip() == "").sum() if text_src in df.columns else 0
        total_blank = int(na + empty)
        if total_blank > 0:
            flags["blank_text_rows"] = total_blank

    # Duplicate post_id
    pid_src = canonical_to_source.get("post_id")
    if pid_src and pid_src in df.columns:
        dup = int(df[pid_src].duplicated().sum())
        if dup:
            flags["duplicate_post_ids"] = dup
    else:
        flags["missing_post_id"] = True

    # Unparseable dates
    date_src = canonical_to_source.get("created_at")
    if date_src and date_src in df.columns:
        parsed = pd.to_datetime(df[date_src], errors="coerce", utc=False)
        bad = int(parsed.isna().sum()) - int(df[date_src].isna().sum())
        if bad > 0:
            flags["unparseable_dates"] = bad

    return flags


def preview_rows(df: pd.DataFrame, n: int = 5) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    head = df.head(n).copy()
    # Ensure JSON-safe values
    for col in head.columns:
        head[col] = head[col].astype(object).where(head[col].notna(), None)
        head[col] = head[col].apply(lambda v: v.isoformat() if hasattr(v, "isoformat") else v)
    return head.to_dict(orient="records")
