"""Codebook helpers — create, edit, import/export.

A category is a dict with these keys (see also AppState.Codebook):
    cat_id          — stable slug, e.g. "hate_speech"
    title           — display label, e.g. "Hate speech"
    description     — free text, shown in the coding UI as a tooltip
    exclusion_group — if two categories share a non-empty group, a row can
                      carry only one of them (the new tag evicts its siblings).
    shortcut_key    — single character (a-z, 0-9) used for keyboard coding.
                      Empty = no shortcut. Must be unique within a codebook.
    color           — "#RRGGBB" tint for the button (optional).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from corpus_intel.app_state import Codebook

SCHEMA_VERSION = 1
CAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
SHORTCUT_RE = re.compile(r"^[a-z0-9]$")   # single char lowercase


class CodebookError(ValueError):
    """Raised for validation failures."""


# ─── Identity ────────────────────────────────────────────────────────────────
def slugify_cat_id(title: str) -> str:
    s = (title or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        s = "cat_" + uuid.uuid4().hex[:6]
    return s[:48]


def new_codebook(name: str, version: str = "1.0") -> Codebook:
    name = (name or "").strip()
    if not name:
        raise CodebookError("Codebook name cannot be empty.")
    return Codebook(
        codebook_id=f"cb_{uuid.uuid4().hex[:10]}",
        name=name,
        version=(version or "1.0").strip(),
        categories=[],
        created_at=dt.datetime.now().isoformat(timespec="seconds"),
    )


def _blank_category() -> Dict[str, Any]:
    return {"cat_id": "", "title": "", "description": "",
            "exclusion_group": "", "shortcut_key": "", "color": ""}


# ─── Categories ──────────────────────────────────────────────────────────────
def add_category(
    cb: Codebook,
    title: str,
    *,
    description: str = "",
    exclusion_group: str = "",
    shortcut_key: str = "",
    color: str = "",
    cat_id: Optional[str] = None,
) -> Dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise CodebookError("Category title cannot be empty.")

    cat_id = (cat_id or slugify_cat_id(title)).strip().lower()
    if not CAT_ID_RE.match(cat_id):
        raise CodebookError(f"Invalid category id: '{cat_id}'. Use lowercase letters, digits, _ or -.")
    if any(c["cat_id"] == cat_id for c in cb.categories):
        raise CodebookError(f"A category with id '{cat_id}' already exists.")

    key = (shortcut_key or "").strip().lower()
    if key and not SHORTCUT_RE.match(key):
        raise CodebookError("Shortcut must be a single a–z or 0–9 character.")
    if key and any(c.get("shortcut_key", "").lower() == key for c in cb.categories):
        raise CodebookError(f"Shortcut '{key}' is already used in this codebook.")

    cat = _blank_category()
    cat.update({
        "cat_id": cat_id,
        "title": title,
        "description": (description or "").strip(),
        "exclusion_group": (exclusion_group or "").strip(),
        "shortcut_key": key,
        "color": (color or "").strip(),
    })
    cb.categories.append(cat)
    return cat


def update_category(
    cb: Codebook,
    cat_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    exclusion_group: Optional[str] = None,
    shortcut_key: Optional[str] = None,
    color: Optional[str] = None,
) -> Dict[str, Any]:
    cat = _find_category(cb, cat_id)

    if title is not None:
        t = title.strip()
        if not t:
            raise CodebookError("Category title cannot be empty.")
        cat["title"] = t
    if description is not None:
        cat["description"] = description.strip()
    if exclusion_group is not None:
        cat["exclusion_group"] = exclusion_group.strip()
    if shortcut_key is not None:
        key = shortcut_key.strip().lower()
        if key and not SHORTCUT_RE.match(key):
            raise CodebookError("Shortcut must be a single a–z or 0–9 character.")
        if key and any(c.get("shortcut_key", "").lower() == key and c["cat_id"] != cat_id for c in cb.categories):
            raise CodebookError(f"Shortcut '{key}' is already used in this codebook.")
        cat["shortcut_key"] = key
    if color is not None:
        cat["color"] = color.strip()
    return cat


def remove_category(cb: Codebook, cat_id: str) -> None:
    before = len(cb.categories)
    cb.categories = [c for c in cb.categories if c["cat_id"] != cat_id]
    if len(cb.categories) == before:
        raise CodebookError(f"No category with id '{cat_id}'.")


def reorder_categories(cb: Codebook, cat_ids: List[str]) -> None:
    if sorted(cat_ids) != sorted(c["cat_id"] for c in cb.categories):
        raise CodebookError("Reorder list must contain exactly the current category ids.")
    index = {c["cat_id"]: c for c in cb.categories}
    cb.categories = [index[cid] for cid in cat_ids]


def _find_category(cb: Codebook, cat_id: str) -> Dict[str, Any]:
    for c in cb.categories:
        if c["cat_id"] == cat_id:
            return c
    raise CodebookError(f"No category with id '{cat_id}'.")


def get_category(cb: Codebook, cat_id: str) -> Optional[Dict[str, Any]]:
    for c in cb.categories:
        if c["cat_id"] == cat_id:
            return c
    return None


# ─── Validation ──────────────────────────────────────────────────────────────
def shortcut_conflicts(cb: Codebook) -> Dict[str, List[str]]:
    """Return {shortcut_key -> [cat_id…]} for keys used more than once."""
    hits: Dict[str, List[str]] = {}
    for c in cb.categories:
        k = (c.get("shortcut_key") or "").lower()
        if not k:
            continue
        hits.setdefault(k, []).append(c["cat_id"])
    return {k: v for k, v in hits.items() if len(v) > 1}


def validate(cb: Codebook) -> List[str]:
    """Return a list of warning strings (empty = clean)."""
    warnings: List[str] = []
    ids = [c["cat_id"] for c in cb.categories]
    dups = {i for i in ids if ids.count(i) > 1}
    for d in dups:
        warnings.append(f"Duplicate category id: {d}")
    conflicts = shortcut_conflicts(cb)
    for k, winners in conflicts.items():
        warnings.append(f"Shortcut '{k}' used by multiple categories: {', '.join(winners)}")
    for c in cb.categories:
        if not CAT_ID_RE.match(c.get("cat_id", "")):
            warnings.append(f"Invalid cat_id: '{c.get('cat_id')}'")
        if not c.get("title"):
            warnings.append(f"Empty title for {c.get('cat_id')}")
    return warnings


# ─── Import / export ─────────────────────────────────────────────────────────
def export_dict(cb: Codebook) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "corpus_intel.codebook",
        "codebook_id": cb.codebook_id,
        "name": cb.name,
        "version": cb.version,
        "created_at": cb.created_at,
        "categories": [dict(c) for c in cb.categories],
    }


def export_json(cb: Codebook, *, indent: int = 2) -> str:
    return json.dumps(export_dict(cb), ensure_ascii=False, indent=indent)


def import_dict(payload: Dict[str, Any], *, new_id: bool = True) -> Codebook:
    if not isinstance(payload, dict):
        raise CodebookError("Codebook JSON must be an object.")
    if payload.get("kind") not in (None, "corpus_intel.codebook"):
        raise CodebookError(f"Unrecognised 'kind' field: {payload.get('kind')}")
    name = (payload.get("name") or "").strip()
    if not name:
        raise CodebookError("Imported codebook has no name.")
    cb = Codebook(
        codebook_id=f"cb_{uuid.uuid4().hex[:10]}" if new_id else payload.get("codebook_id", f"cb_{uuid.uuid4().hex[:10]}"),
        name=name,
        version=(payload.get("version") or "1.0").strip(),
        categories=[],
        created_at=payload.get("created_at") or dt.datetime.now().isoformat(timespec="seconds"),
    )
    for raw in payload.get("categories", []) or []:
        cat = _blank_category()
        for k in cat:
            if k in raw:
                cat[k] = raw[k] if raw[k] is not None else cat[k]
        if not cat.get("cat_id"):
            cat["cat_id"] = slugify_cat_id(cat.get("title", ""))
        cb.categories.append(cat)
    # De-dupe shortcut collisions on import by clearing the later one(s).
    seen_shortcuts: set = set()
    for c in cb.categories:
        k = (c.get("shortcut_key") or "").lower()
        if k and k in seen_shortcuts:
            c["shortcut_key"] = ""
        elif k:
            seen_shortcuts.add(k)
    return cb


def import_json(text: str, *, new_id: bool = True) -> Codebook:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise CodebookError(f"Invalid JSON: {e.msg} (line {e.lineno}, col {e.colno})") from e
    return import_dict(payload, new_id=new_id)


# ─── Preset starter ──────────────────────────────────────────────────────────
STARTER_HATE_SPEECH: List[Tuple[str, str, str, str, str]] = [
    # (cat_id, title, description, exclusion_group, shortcut)
    ("hate_speech",     "Hate speech",      "Dehumanising attacks on a protected group.",       "severity", "h"),
    ("harassment",      "Harassment",       "Targeted intimidation or threats against individuals.", "severity", "a"),
    ("misinformation",  "Misinformation",   "Factually false or misleading content.",           "",         "m"),
    ("legitimate",      "Legitimate speech","Content that does not fit any of the above.",      "severity", "l"),
    ("unsure",          "Unsure / unclear", "Ambiguous — needs second opinion.",                "severity", "u"),
]


def starter_hate_speech() -> Codebook:
    cb = new_codebook("Hate speech starter", version="1.0")
    for cid, title, desc, group, key in STARTER_HATE_SPEECH:
        add_category(cb, title, description=desc, exclusion_group=group, shortcut_key=key, cat_id=cid)
    return cb
