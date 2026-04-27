"""AI-coding answer cache — avoid re-paying for the same row.

Cache key is a SHA-1 over (text, codebook_id, codebook_version, prompt_version,
sorted cat_ids). When any of those change, the entry becomes invalid (the caller
will miss and re-classify). Values are stored as JSON on disk.

Schema of the on-disk file (sessions/cache/ai_coding.json):
{
  "version": 1,
  "entries": {
      "<sha1>": {
          "cats": ["hate_speech"],
          "confidence": 0.82,
          "reason": "dehumanising slur aimed at ethnic group",
          "model": "claude-haiku-4-5-20251001",
          "ts": "2026-04-17T18:45:01"
      },
      ...
  }
}

Never call any LLM from here. Pure I/O + hashing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional

from corpus_intel.constants import CACHE_DIR

log = logging.getLogger("corpus_intel.answer_cache")

CACHE_FILE = os.path.join(CACHE_DIR, "ai_coding.json")
CACHE_SCHEMA_VERSION = 1
_LOCK = threading.Lock()

# In-process cache of the JSON file — loaded lazily, kept in sync on every
# store. Avoids re-parsing the full JSON on every classification batch; at
# 100k cached entries the parse alone was dominating run time.
_MEM: Optional[Dict[str, Any]] = None


# ─── Key ───────────────────────────────────────────────────────────────────
def row_key(
    text: str,
    codebook_id: str,
    codebook_version: str,
    prompt_version: str,
    cat_ids: Iterable[str],
) -> str:
    cats_joined = ",".join(sorted(cat_ids))
    blob = f"{codebook_id}|{codebook_version}|{prompt_version}|{cats_joined}|{text or ''}"
    return hashlib.sha1(blob.encode("utf-8", errors="ignore")).hexdigest()


# ─── Load / save ───────────────────────────────────────────────────────────
def _read_from_disk() -> Dict[str, Any]:
    if not os.path.exists(CACHE_FILE):
        return {"version": CACHE_SCHEMA_VERSION, "entries": {}}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": CACHE_SCHEMA_VERSION, "entries": {}}
        return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("answer cache unreadable, starting fresh: %s", e)
        return {"version": CACHE_SCHEMA_VERSION, "entries": {}}


def _get_mem() -> Dict[str, Any]:
    """Return the in-process cache, loading from disk on first access.

    Caller must hold `_LOCK` when mutating the returned dict."""
    global _MEM
    if _MEM is None:
        _MEM = _read_from_disk()
        if not isinstance(_MEM.get("entries"), dict):
            _MEM["entries"] = {}
    return _MEM


def _save(data: Dict[str, Any]) -> None:
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError as e:
        log.warning("answer cache save failed: %s", e)


# ─── Public API ────────────────────────────────────────────────────────────
def lookup_many(keys: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    with _LOCK:
        entries = _get_mem().get("entries", {})
        return {k: entries[k] for k in keys if k in entries}


def store_many(items: Dict[str, Dict[str, Any]], model: str = "") -> None:
    if not items:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _LOCK:
        data = _get_mem()
        entries = data.setdefault("entries", {})
        for k, v in items.items():
            entries[k] = {
                "cats": list(v.get("cats") or []),
                "confidence": float(v.get("confidence") or 0.0),
                "reason": str(v.get("reason") or "")[:200],
                "model": model or v.get("model", ""),
                "ts": now,
            }
        data["version"] = CACHE_SCHEMA_VERSION
        _save(data)


def stats() -> Dict[str, Any]:
    with _LOCK:
        entries = _get_mem().get("entries", {}) or {}
        return {"entries": len(entries), "path": CACHE_FILE}


def clear() -> int:
    """Wipe the cache. Returns the number of entries removed."""
    global _MEM
    with _LOCK:
        data = _get_mem()
        n = len(data.get("entries", {}) or {})
        data["entries"] = {}
        _save(data)
    return n


def reload_from_disk() -> None:
    """Drop the in-process cache so the next access re-reads the file.

    Call after a session reset or any out-of-band cache file swap."""
    global _MEM
    with _LOCK:
        _MEM = None
