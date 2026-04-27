"""AI-run checkpoints — snapshot tags + ai_classifications before a run, allow revert.

Each checkpoint is a JSONL line under sessions/ai_checkpoints/{codebook_id}.jsonl
with the full tags + ai_classifications maps frozen at that moment. Reverting
restores both maps in place.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from copy import deepcopy
from typing import Any, Dict, List, Optional

from corpus_intel.app_state import AppState
from corpus_intel.constants import SESSION_DIR

log = logging.getLogger("corpus_intel.ai_checkpoints")

_DIR = os.path.join(SESSION_DIR, "ai_checkpoints")
_lock = threading.Lock()
KEEP = 10  # most-recent N per codebook


def _path(codebook_id: str) -> str:
    return os.path.join(_DIR, f"{codebook_id or '_no_cb'}.jsonl")


def snapshot(state: AppState, *, codebook_id: str, note: str = "") -> Dict[str, Any]:
    """Freeze the current tags + ai_classifications. Returns the entry."""
    os.makedirs(_DIR, exist_ok=True)
    entry = {
        "id": dt.datetime.now().strftime("ck_%Y%m%dT%H%M%S%f")[:18],
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "codebook_id": codebook_id or "",
        "note": (note or "").strip(),
        "tags": deepcopy(state.tags),
        "ai_classifications": deepcopy(state.ai_classifications),
        "tag_count": sum(len(v) for v in state.tags.values()),
    }
    with _lock:
        with open(_path(codebook_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        _prune(codebook_id)
    return {k: v for k, v in entry.items() if k not in ("tags", "ai_classifications")}


def list_checkpoints(codebook_id: str) -> List[Dict[str, Any]]:
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
                    e = json.loads(line)
                    out.append({k: v for k, v in e.items() if k not in ("tags", "ai_classifications")})
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def revert(state: AppState, *, codebook_id: str, checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """Restore tags + ai_classifications from the named checkpoint. Returns summary."""
    p = _path(codebook_id)
    if not os.path.exists(p):
        return None
    target: Optional[Dict[str, Any]] = None
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("id") == checkpoint_id:
                    target = e
                    break
    except OSError:
        return None
    if not target:
        return None
    state.tags = deepcopy(target.get("tags") or {})
    state.ai_classifications = deepcopy(target.get("ai_classifications") or {})
    # Reverting is a major mutation — clear undo/redo since they're pre-revert.
    if hasattr(state, "_undo_stack"):
        state._undo_stack.clear()
    if hasattr(state, "_redo_stack"):
        state._redo_stack.clear()
    return {
        "checkpoint_id": checkpoint_id,
        "ts": target.get("ts"),
        "note": target.get("note", ""),
        "tag_count": target.get("tag_count", 0),
    }


def _prune(codebook_id: str) -> None:
    p = _path(codebook_id)
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        if len(lines) <= KEEP:
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-KEEP:]) + "\n")
    except OSError:
        pass
