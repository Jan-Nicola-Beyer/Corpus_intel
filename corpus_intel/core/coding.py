"""Manual coding operations — tag/untag rows, bulk-tag by query, undo stack.

All tags live on AppState.tags:
    tags[row_idx_str] = [
        {"cat_id": "hate_speech", "coder": "alice", "ts": "2026-04-17T17:55:01",
         "source": "manual", "codebook_id": "cb_…"},
        ...
    ]

Key rules:
    * A coder cannot stack two tags from the same exclusion_group on one row.
      Tagging with a new category from the same group replaces the prior one.
    * Multiple coders can place overlapping tags on the same row — that's how
      IRR is computed later.
    * All mutations push a compact patch onto AppState._undo_stack (max 50).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Optional

from corpus_intel.app_state import AppState, Codebook, ProvenanceEvent
from corpus_intel.core.codebook import get_category
from corpus_intel.core import undo_log

UNDO_MAX = 50


class CodingError(ValueError):
    """Raised when a tagging request is invalid (missing coder, bad cat id, …)."""


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _as_key(row_idx: Any) -> str:
    """Tag keys are strings because JSON dicts don't keep int keys."""
    return str(int(row_idx))


def _require_coder(coder: str) -> str:
    coder = (coder or "").strip()
    if not coder:
        raise CodingError(
            "No coder identified. Set your coder name in Settings before coding."
        )
    return coder


def _require_active(state: AppState) -> Codebook:
    if not state.active_codebook or state.active_codebook not in state.codebooks:
        raise CodingError("No active codebook. Pick or create one in the Codebook section.")
    return state.codebooks[state.active_codebook]


# ─── Core tag/untag ─────────────────────────────────────────────────────────
def tag_row(
    state: AppState,
    row_idx: int,
    cat_id: str,
    coder: str,
    *,
    source: str = "manual",
    note: Optional[str] = None,
    push_undo: bool = True,
) -> Dict[str, Any]:
    """Attach a single tag. Returns an 'undo-patch' describing the mutation."""
    coder = _require_coder(coder)
    cb = _require_active(state)
    cat = get_category(cb, cat_id)
    if cat is None:
        raise CodingError(f"Category '{cat_id}' is not in the active codebook.")

    key = _as_key(row_idx)
    entries: List[Dict[str, Any]] = state.tags.get(key, [])
    note = (note or "").strip() or None

    # If category is in an exclusion group, remove any sibling tags this coder
    # already placed from the same group (within the active codebook).
    exgrp = (cat.get("exclusion_group") or "").strip()
    removed: List[Dict[str, Any]] = []
    if exgrp:
        sibling_ids = {
            c["cat_id"] for c in cb.categories
            if (c.get("exclusion_group") or "").strip() == exgrp and c["cat_id"] != cat_id
        }
        kept: List[Dict[str, Any]] = []
        for e in entries:
            if (
                e.get("coder") == coder
                and e.get("codebook_id") == cb.codebook_id
                and e.get("cat_id") in sibling_ids
            ):
                removed.append(e)
            else:
                kept.append(e)
        entries = kept

    # No-op if this coder already placed this tag — but update note if it changed.
    already_entry = None
    for e in entries:
        if (
            e.get("coder") == coder
            and e.get("cat_id") == cat_id
            and e.get("codebook_id") == cb.codebook_id
        ):
            already_entry = e
            break
    added: List[Dict[str, Any]] = []
    if already_entry is None:
        new_tag = {
            "cat_id": cat_id,
            "coder": coder,
            "ts": _now_iso(),
            "source": source,
            "codebook_id": cb.codebook_id,
        }
        if note:
            new_tag["note"] = note
        entries.append(new_tag)
        added.append(new_tag)
    elif note is not None and already_entry.get("note") != note:
        # Edit-in-place for memo updates; don't push undo for a note-only change.
        already_entry["note"] = note

    if entries:
        state.tags[key] = entries
    else:
        state.tags.pop(key, None)

    patch = {
        "action": "tag",
        "row_idx": int(row_idx),
        "added": added,
        "removed": removed,
        "ts": _now_iso(),
    }
    if push_undo and (added or removed):
        _push_undo(state, patch)
    return patch


def untag_row(
    state: AppState,
    row_idx: int,
    cat_id: str,
    coder: str,
    *,
    push_undo: bool = True,
) -> Dict[str, Any]:
    coder = _require_coder(coder)
    cb = _require_active(state)
    key = _as_key(row_idx)
    entries = state.tags.get(key, [])

    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    for e in entries:
        if (
            e.get("coder") == coder
            and e.get("cat_id") == cat_id
            and e.get("codebook_id") == cb.codebook_id
        ):
            removed.append(e)
        else:
            kept.append(e)
    if kept:
        state.tags[key] = kept
    else:
        state.tags.pop(key, None)

    patch = {
        "action": "untag",
        "row_idx": int(row_idx),
        "added": [],
        "removed": removed,
        "ts": _now_iso(),
    }
    if push_undo and removed:
        _push_undo(state, patch)
    return patch


# ─── Bulk tag ────────────────────────────────────────────────────────────────
def bulk_tag(
    state: AppState,
    row_indices: Iterable[int],
    cat_id: str,
    coder: str,
    *,
    source: str = "bulk",
) -> Dict[str, Any]:
    """Apply the same tag to many rows in one stroke. Single undo step covers all."""
    coder = _require_coder(coder)
    cb = _require_active(state)
    if get_category(cb, cat_id) is None:
        raise CodingError(f"Category '{cat_id}' is not in the active codebook.")

    batch: List[Dict[str, Any]] = []
    touched = 0
    for r in row_indices:
        patch = tag_row(state, int(r), cat_id, coder, source=source, push_undo=False)
        if patch["added"] or patch["removed"]:
            touched += 1
            batch.append({
                "row_idx": patch["row_idx"],
                "added": patch["added"],
                "removed": patch["removed"],
            })

    combined = {
        "action": "bulk",
        "cat_id": cat_id,
        "coder": coder,
        "source": source,
        "batch": batch,
        "ts": _now_iso(),
    }
    if batch:
        _push_undo(state, combined)

    state.provenance.append(
        ProvenanceEvent(
            ts=combined["ts"],
            action="bulk_tag",
            params={
                "cat_id": cat_id, "coder": coder, "source": source,
                "rows_affected": touched, "codebook_id": cb.codebook_id,
            },
        )
    )
    return {"rows_affected": touched, "cat_id": cat_id, "coder": coder}


# ─── Undo / redo ─────────────────────────────────────────────────────────────
def _push_undo(state: AppState, patch: Dict[str, Any]) -> None:
    state._undo_stack.append(patch)
    if len(state._undo_stack) > UNDO_MAX:
        state._undo_stack = state._undo_stack[-UNDO_MAX:]
    # New mutation invalidates the redo branch.
    if getattr(state, "_redo_stack", None):
        state._redo_stack.clear()
    undo_log.append(patch)


def undo_last(state: AppState) -> Optional[Dict[str, Any]]:
    """Reverse the most recent tag/untag/bulk op. Returns the applied inverse."""
    if not state._undo_stack:
        return None
    patch = state._undo_stack.pop()
    undo_log.pop_last()
    action = patch.get("action")
    if action in ("tag", "untag"):
        _apply_inverse_single(state, patch)
        state._redo_stack.append(patch)
        return {"action": "undo", "of": action, "row_idx": patch.get("row_idx")}
    if action == "bulk":
        for entry in patch.get("batch", []):
            _apply_inverse_single(state, {"added": entry["added"], "removed": entry["removed"], "row_idx": entry["row_idx"]})
        state._redo_stack.append(patch)
        return {"action": "undo", "of": "bulk", "rows": len(patch.get("batch", []))}
    return None


def redo_last(state: AppState) -> Optional[Dict[str, Any]]:
    """Re-apply the most recently undone op. Returns the re-applied patch summary."""
    redo_stack = getattr(state, "_redo_stack", None) or []
    if not redo_stack:
        return None
    patch = state._redo_stack.pop()
    action = patch.get("action")
    # Re-apply the original direction (added → add, removed → remove).
    if action in ("tag", "untag"):
        _apply_forward_single(state, patch)
        state._undo_stack.append(patch)
        undo_log.append(patch)
        return {"action": "redo", "of": action, "row_idx": patch.get("row_idx")}
    if action == "bulk":
        for entry in patch.get("batch", []):
            _apply_forward_single(state, {"added": entry["added"], "removed": entry["removed"], "row_idx": entry["row_idx"]})
        state._undo_stack.append(patch)
        undo_log.append(patch)
        return {"action": "redo", "of": "bulk", "rows": len(patch.get("batch", []))}
    return None


def _apply_forward_single(state: AppState, patch: Dict[str, Any]) -> None:
    key = _as_key(patch["row_idx"])
    entries = state.tags.get(key, [])
    # Remove what was originally removed.
    for tag in patch.get("removed", []):
        entries = [e for e in entries if not _same_entry(e, tag)]
    # Add back what was originally added.
    for tag in patch.get("added", []):
        if not any(_same_entry(e, tag) for e in entries):
            entries.append(tag)
    if entries:
        state.tags[key] = entries
    else:
        state.tags.pop(key, None)


def _apply_inverse_single(state: AppState, patch: Dict[str, Any]) -> None:
    key = _as_key(patch["row_idx"])
    entries = state.tags.get(key, [])

    # Remove what was added (match by coder + cat_id + codebook_id + ts).
    for tag in patch.get("added", []):
        entries = [e for e in entries if not _same_entry(e, tag)]
    # Re-add what was removed.
    for tag in patch.get("removed", []):
        if not any(_same_entry(e, tag) for e in entries):
            entries.append(tag)
    if entries:
        state.tags[key] = entries
    else:
        state.tags.pop(key, None)


def _same_entry(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (
        a.get("cat_id") == b.get("cat_id")
        and a.get("coder") == b.get("coder")
        and a.get("codebook_id") == b.get("codebook_id")
        and a.get("ts") == b.get("ts")
    )


# ─── Reporting ───────────────────────────────────────────────────────────────
def row_tags(state: AppState, row_idx: int) -> List[Dict[str, Any]]:
    return list(state.tags.get(_as_key(row_idx), []))


def progress(state: AppState, row_indices: Optional[Iterable[int]] = None) -> Dict[str, Any]:
    """Summarise how much of the given row scope has been coded in the active
    codebook. Scope defaults to the whole tags table."""
    cb_id = state.active_codebook or ""
    if row_indices is None:
        keys = list(state.tags.keys())
    else:
        keys = [_as_key(r) for r in row_indices]

    tagged_rows = 0
    by_category: Dict[str, int] = {}
    by_coder: Dict[str, int] = {}
    for k in keys:
        entries = [e for e in state.tags.get(k, []) if not cb_id or e.get("codebook_id") == cb_id]
        if entries:
            tagged_rows += 1
        seen_coders = set()
        for e in entries:
            by_category[e.get("cat_id", "?")] = by_category.get(e.get("cat_id", "?"), 0) + 1
            c = e.get("coder", "?")
            if c not in seen_coders:
                by_coder[c] = by_coder.get(c, 0) + 1
                seen_coders.add(c)

    total_rows = len(keys) if row_indices is not None else None
    return {
        "codebook_id": cb_id,
        "rows_in_scope": total_rows,
        "rows_tagged": tagged_rows,
        "by_category": by_category,
        "by_coder": by_coder,
        "undo_available": len(state._undo_stack),
    }


def all_coders(state: AppState) -> List[str]:
    out: set = set()
    for entries in state.tags.values():
        for e in entries:
            if e.get("coder"):
                out.add(e["coder"])
    return sorted(out)
