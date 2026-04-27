"""Codebook version history — immutable snapshots + diff (P11).

Each time the user confirms a codebook edit, we append a snapshot keyed by
(codebook_id, version_index). Snapshots are just `export_dict(cb)` payloads with
a monotonic index.

Disk layout: sessions/codebook_versions/{codebook_id}.jsonl
Each line is one version's JSON.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from corpus_intel.app_state import Codebook
from corpus_intel.constants import SESSION_DIR
from corpus_intel.core import codebook as _cb_mod

log = logging.getLogger("corpus_intel.codebook_versions")

_DIR = os.path.join(SESSION_DIR, "codebook_versions")
_lock = threading.Lock()


def _path(codebook_id: str) -> str:
    return os.path.join(_DIR, f"{codebook_id}.jsonl")


def snapshot(cb: Codebook, *, note: str = "") -> Dict[str, Any]:
    """Append the current codebook state as a new version. Returns the entry."""
    os.makedirs(_DIR, exist_ok=True)
    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "note": (note or "").strip(),
        "index": 0,  # filled in below
        "payload": _cb_mod.export_dict(cb),
    }
    with _lock:
        existing = list_versions(cb.codebook_id)
        entry["index"] = len(existing) + 1
        with open(_path(cb.codebook_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def list_versions(codebook_id: str) -> List[Dict[str, Any]]:
    p = _path(codebook_id)
    if not os.path.exists(p):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def get_version(codebook_id: str, index: int) -> Optional[Dict[str, Any]]:
    versions = list_versions(codebook_id)
    if not versions:
        return None
    # Accept negative indices: -1 = latest, -2 = previous, …
    if int(index) < 0:
        try:
            return versions[int(index)]
        except IndexError:
            return None
    for v in versions:
        if int(v.get("index") or 0) == int(index):
            return v
    return None


def diff(codebook_id: str, a_index: int, b_index: int) -> Dict[str, Any]:
    """Return a shallow category-level diff between versions (added/removed/changed).
    Negative indices count from the latest (-1 = latest, -2 = previous)."""
    versions = list_versions(codebook_id)
    available = [int(v.get("index") or 0) for v in versions]
    a = get_version(codebook_id, a_index)
    b = get_version(codebook_id, b_index)
    if not a or not b:
        return {
            "error": "one or both versions not found",
            "requested": {"a": a_index, "b": b_index},
            "available_indices": available,
        }
    cats_a = {c["cat_id"]: c for c in (a["payload"].get("categories") or [])}
    cats_b = {c["cat_id"]: c for c in (b["payload"].get("categories") or [])}
    added = [cats_b[k] for k in cats_b if k not in cats_a]
    removed = [cats_a[k] for k in cats_a if k not in cats_b]
    changed: List[Dict[str, Any]] = []
    for k in cats_a:
        if k in cats_b and cats_a[k] != cats_b[k]:
            fields = {}
            for f in set(cats_a[k]) | set(cats_b[k]):
                if cats_a[k].get(f) != cats_b[k].get(f):
                    fields[f] = {"from": cats_a[k].get(f), "to": cats_b[k].get(f)}
            changed.append({"cat_id": k, "fields": fields})
    return {
        "from": {"index": a["index"], "ts": a["ts"], "note": a.get("note", "")},
        "to":   {"index": b["index"], "ts": b["ts"], "note": b.get("note", "")},
        "added": added, "removed": removed, "changed": changed,
    }
