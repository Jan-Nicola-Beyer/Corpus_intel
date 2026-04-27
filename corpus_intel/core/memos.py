"""Per-row free-text memos — the inductive/grounded-theory side of coding.

Where `tags` answer "which category?", memos answer "what did I notice?".
Keyed by row index on AppState.memos. Each memo:
    {memo_id, text, coder, ts, codebook_id}

A coder may write multiple memos per row; memos never participate in the
undo stack or IRR calculations.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Dict, Iterable, List, Optional

from corpus_intel.app_state import AppState


class MemoError(ValueError):
    """Raised when a memo request is malformed."""


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _as_key(row_idx: Any) -> str:
    return str(int(row_idx))


def add_memo(
    state: AppState,
    row_idx: int,
    text: str,
    coder: str,
    *,
    codebook_id: str = "",
) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise MemoError("Memo text is empty.")
    coder = (coder or "").strip() or "anonymous"
    entry = {
        "memo_id": uuid.uuid4().hex[:12],
        "text": text,
        "coder": coder,
        "ts": _now_iso(),
        "codebook_id": codebook_id or "",
    }
    key = _as_key(row_idx)
    state.memos.setdefault(key, []).append(entry)
    return entry


def edit_memo(state: AppState, row_idx: int, memo_id: str, text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        raise MemoError("Memo text is empty.")
    key = _as_key(row_idx)
    for e in state.memos.get(key, []):
        if e.get("memo_id") == memo_id:
            e["text"] = text
            e["ts"] = _now_iso()
            return e
    return None


def delete_memo(state: AppState, row_idx: int, memo_id: str) -> bool:
    key = _as_key(row_idx)
    entries = state.memos.get(key, [])
    kept = [e for e in entries if e.get("memo_id") != memo_id]
    if len(kept) == len(entries):
        return False
    if kept:
        state.memos[key] = kept
    else:
        state.memos.pop(key, None)
    return True


def row_memos(state: AppState, row_idx: int) -> List[Dict[str, Any]]:
    return list(state.memos.get(_as_key(row_idx), []))


def list_all(
    state: AppState,
    *,
    coder: Optional[str] = None,
    codebook_id: Optional[str] = None,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Flat list of all memos with row_idx attached, newest first.

    `query` does a case-insensitive substring match against memo text.
    """
    q = (query or "").strip().lower() or None
    out: List[Dict[str, Any]] = []
    for k, entries in (state.memos or {}).items():
        for e in entries:
            if coder and e.get("coder") != coder:
                continue
            if codebook_id and e.get("codebook_id") != codebook_id:
                continue
            if q and q not in (e.get("text") or "").lower():
                continue
            rec = dict(e)
            try:
                rec["row_idx"] = int(k)
            except (TypeError, ValueError):
                rec["row_idx"] = k
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def counts(state: AppState) -> Dict[str, Any]:
    total = 0
    by_coder: Dict[str, int] = {}
    for entries in (state.memos or {}).values():
        for e in entries:
            total += 1
            c = e.get("coder", "?")
            by_coder[c] = by_coder.get(c, 0) + 1
    return {"total": total, "rows_with_memo": len(state.memos or {}), "by_coder": by_coder}
