"""Span-level annotations (P11) — tag character ranges of a row, not the whole post.

Model
-----
An annotation is:
    { row_id, start, end, cat_id, coder, ts, note? }

Storage: sessions/span_annotations.jsonl (append-only).

This is a data layer only — the UI for highlighting ranges lives in app.js.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from corpus_intel.constants import SESSION_DIR

log = logging.getLogger("corpus_intel.spans")

_PATH = os.path.join(SESSION_DIR, "span_annotations.jsonl")
_lock = threading.Lock()


def _validate(row_id: str, start: int, end: int, cat_id: str) -> None:
    if not row_id:
        raise ValueError("row_id required")
    if start < 0 or end <= start:
        raise ValueError(f"bad span range: {start}-{end}")
    if not cat_id:
        raise ValueError("cat_id required")


def add(row_id: str, start: int, end: int, cat_id: str, *,
        coder: str = "", note: str = "") -> Dict[str, Any]:
    _validate(row_id, start, end, cat_id)
    entry = {
        "row_id": str(row_id),
        "start": int(start),
        "end": int(end),
        "cat_id": str(cat_id),
        "coder": str(coder),
        "note": str(note or "")[:500],
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_for_row(row_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.exists(_PATH):
        return out
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("row_id") == str(row_id):
                        out.append(obj)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def list_all(limit: int = 1000) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.exists(_PATH):
        return out
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out[-limit:]


def remove(row_id: str, start: int, end: int, cat_id: str) -> bool:
    """Rewrite the file without the given annotation. Returns True if removed."""
    if not os.path.exists(_PATH):
        return False
    removed = False
    kept: List[str] = []
    with _lock:
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    if (obj.get("row_id") == str(row_id)
                            and int(obj.get("start") or -1) == int(start)
                            and int(obj.get("end") or -1) == int(end)
                            and obj.get("cat_id") == cat_id
                            and not removed):
                        removed = True
                        continue
                    kept.append(s)
            if removed:
                tmp = _PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for s in kept:
                        f.write(s + "\n")
                os.replace(tmp, _PATH)
        except OSError:
            return False
    return removed
