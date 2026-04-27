"""Persistent undo log — append-only JSONL that survives reload.

Each line is a JSON patch compatible with the in-memory undo stack
(see core/coding.py `_push_undo` payload shape). Rolling-capped at
UNDO_LOG_MAX; on append past the cap, the oldest N lines are trimmed.

Not a database; a human-readable trail for the user's own safety.
Universal undo (Phase 14) will reuse the same file with richer action
payloads.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List

from corpus_intel.constants import UNDO_LOG_FILE, UNDO_LOG_MAX

log = logging.getLogger("corpus_intel.undo")
_lock = threading.Lock()


def append(patch: Dict[str, Any]) -> None:
    """Append one undo event to the log, trim if past cap."""
    try:
        with _lock:
            os.makedirs(os.path.dirname(UNDO_LOG_FILE), exist_ok=True)
            with open(UNDO_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(patch, ensure_ascii=False, default=str) + "\n")
            _maybe_trim()
    except Exception:
        log.exception("undo append failed")


def _maybe_trim() -> None:
    """Cheap check: if line count > 1.25 * cap, rewrite tail-only."""
    try:
        if not os.path.exists(UNDO_LOG_FILE):
            return
        # fast line count
        with open(UNDO_LOG_FILE, "rb") as f:
            lines = sum(1 for _ in f)
        if lines <= int(UNDO_LOG_MAX * 1.25):
            return
        with open(UNDO_LOG_FILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        tail = all_lines[-UNDO_LOG_MAX:]
        tmp = UNDO_LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp, UNDO_LOG_FILE)
    except Exception:
        log.exception("undo trim failed")


def load_tail(limit: int = UNDO_LOG_MAX) -> List[Dict[str, Any]]:
    """Read the last `limit` events from disk, oldest-first."""
    if not os.path.exists(UNDO_LOG_FILE):
        return []
    try:
        with open(UNDO_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        log.exception("undo load failed")
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def pop_last() -> Dict[str, Any] | None:
    """Remove and return the last event on disk (for universal undo)."""
    if not os.path.exists(UNDO_LOG_FILE):
        return None
    try:
        with _lock:
            with open(UNDO_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return None
            last = lines[-1].strip()
            tmp = UNDO_LOG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[:-1])
            os.replace(tmp, UNDO_LOG_FILE)
            return json.loads(last) if last else None
    except Exception:
        log.exception("undo pop failed")
        return None


def clear() -> None:
    try:
        if os.path.exists(UNDO_LOG_FILE):
            os.remove(UNDO_LOG_FILE)
    except OSError:
        log.exception("undo clear failed")
