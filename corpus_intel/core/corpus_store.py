"""Immutable content-addressed corpus snapshots + a DuckDB/pandas routing façade.

Roadmap refs: SINGLE_USER_ROADMAP P10.1 (store façade) and P10.2 (snapshots).

Model
-----
A *snapshot* is one fully-materialised corpus at one point in time. It is
identified by a short hash of its inputs (dataset ids + dedupe params +
schema version). Rebuilds always produce a *new* snapshot; they never
overwrite an old one. This is how we make slices / tags / topics
reproducible and how we give the user an "undo the build" safety net.

On disk:
    sessions/corpora/{snapshot_id}.parquet      — the row data
    sessions/snapshots.json                     — registry (list of Snapshot dicts)

State glue:
    AppState.settings["active_snapshot_id"]     — currently active snapshot
    AppState.corpus_parquet                     — kept in sync for legacy callers

Routing:
    open_df(snapshot_id)  → returns a pandas.DataFrame. Below ~50k rows we
    read the parquet directly with pandas; above, we route through DuckDB
    which reads the parquet lazily and can evaluate filters in SQL before
    materialising.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from corpus_intel.constants import (
    CORPORA_DIR,
    SNAPSHOTS_FILE,
    DUCKDB_THRESHOLD_ROWS,
)

log = logging.getLogger("corpus_intel.store")

_registry_lock = threading.Lock()
_duck_lock = threading.Lock()
_duck_conn = None  # lazy


# ─── Snapshot dataclass + registry ──────────────────────────────────────────
@dataclass
class Snapshot:
    snapshot_id: str
    name: str
    created_at: str
    parquet_path: str
    rows: int
    schema_version: int = 1
    source_dataset_ids: List[str] = field(default_factory=list)
    dedup_params: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _content_hash(source_ids: List[str], dedup_params: Dict[str, Any], schema_version: int) -> str:
    payload = json.dumps(
        {"s": sorted(source_ids or []), "d": dedup_params or {}, "v": schema_version},
        sort_keys=True, default=str,
    ).encode()
    return hashlib.sha1(payload).hexdigest()[:16]


def _load_registry() -> List[Dict[str, Any]]:
    if not os.path.exists(SNAPSHOTS_FILE):
        return []
    try:
        with open(SNAPSHOTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or []
        return data if isinstance(data, list) else []
    except Exception:
        log.exception("snapshot registry read failed")
        return []


def _save_registry(items: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(SNAPSHOTS_FILE), exist_ok=True)
    tmp = SNAPSHOTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, SNAPSHOTS_FILE)


def list_snapshots() -> List[Snapshot]:
    out = []
    for raw in _load_registry():
        try:
            out.append(Snapshot(**raw))
        except TypeError:
            continue
    out.sort(key=lambda s: s.created_at, reverse=True)
    return out


def get_snapshot(snapshot_id: str) -> Optional[Snapshot]:
    for s in list_snapshots():
        if s.snapshot_id == snapshot_id:
            return s
    return None


# ─── Create / delete ────────────────────────────────────────────────────────
def create_snapshot(
    df: pd.DataFrame,
    *,
    name: str,
    source_dataset_ids: List[str],
    dedup_params: Dict[str, Any],
    stats: Optional[Dict[str, Any]] = None,
    schema_version: int = 1,
) -> Snapshot:
    """Materialise `df` to CORPORA_DIR under a content-addressed id and register it."""
    os.makedirs(CORPORA_DIR, exist_ok=True)
    snap_id = _content_hash(source_dataset_ids, dedup_params, schema_version)
    path = os.path.join(CORPORA_DIR, f"{snap_id}.parquet")
    with _registry_lock:
        # Write parquet (prefer, fall back to pickle)
        try:
            df.to_parquet(path, index=False)
        except (ImportError, ValueError) as e:
            log.warning("parquet write failed (%s); falling back to pickle", e)
            path = os.path.join(CORPORA_DIR, f"{snap_id}.pkl")
            df.to_pickle(path)

        snap = Snapshot(
            snapshot_id=snap_id,
            name=name or f"snapshot-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            created_at=datetime.now().isoformat(timespec="seconds"),
            parquet_path=path,
            rows=int(len(df)),
            schema_version=schema_version,
            source_dataset_ids=list(source_dataset_ids or []),
            dedup_params=dict(dedup_params or {}),
            stats=dict(stats or {}),
        )
        reg = _load_registry()
        # if this content hash already exists, refresh its row count/stats but keep original id
        existing_idx = next((i for i, r in enumerate(reg) if r.get("snapshot_id") == snap_id), -1)
        if existing_idx >= 0:
            reg[existing_idx] = snap.to_dict()
        else:
            reg.append(snap.to_dict())
        _save_registry(reg)
    return snap


def delete_snapshot(snapshot_id: str) -> bool:
    with _registry_lock:
        reg = _load_registry()
        target = next((r for r in reg if r.get("snapshot_id") == snapshot_id), None)
        if not target:
            return False
        reg = [r for r in reg if r.get("snapshot_id") != snapshot_id]
        _save_registry(reg)
        p = target.get("parquet_path")
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                log.warning("could not remove parquet %s", p)
    return True


def purge_snapshots_referencing(dataset_id: str) -> List[str]:
    """Remove every snapshot whose source_dataset_ids contains dataset_id.

    Callers (e.g. force-delete of a dataset) need this because the snapshot's
    parquet still carries rows from a now-missing dataset — activating it
    would resurrect orphaned rows with no backing metadata.

    Returns the list of removed snapshot ids so the caller can log/provenance.
    """
    removed: List[str] = []
    with _registry_lock:
        reg = _load_registry()
        keep: List[Dict[str, Any]] = []
        for r in reg:
            if dataset_id in (r.get("source_dataset_ids") or []):
                removed.append(r.get("snapshot_id") or "")
                p = r.get("parquet_path")
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        log.warning("could not remove parquet %s", p)
                continue
            keep.append(r)
        if removed:
            _save_registry(keep)
    return [rid for rid in removed if rid]


# ─── Read / route (pandas vs. DuckDB) ───────────────────────────────────────
def _read_pandas(path: str) -> pd.DataFrame:
    if path.endswith(".pkl"):
        return pd.read_pickle(path)
    return pd.read_parquet(path)


def _get_duck():
    global _duck_conn
    if _duck_conn is not None:
        return _duck_conn
    with _duck_lock:
        if _duck_conn is None:
            import duckdb
            _duck_conn = duckdb.connect(database=":memory:")
    return _duck_conn


def open_df(snapshot_id: str) -> Optional[pd.DataFrame]:
    """Return the full DataFrame for a snapshot. Below threshold reads with
    pandas; above threshold uses DuckDB's parquet reader (still returns a
    pandas DF, but reading is faster and less memory-hungry for large files)."""
    snap = get_snapshot(snapshot_id)
    if not snap:
        return None
    path = snap.parquet_path
    if not path or not os.path.exists(path):
        return None
    if snap.rows <= DUCKDB_THRESHOLD_ROWS or path.endswith(".pkl"):
        return _read_pandas(path)
    try:
        duck = _get_duck()
        safe = path.replace("'", "''")
        return duck.sql("SELECT * FROM read_parquet('" + safe + "')").to_df()
    except Exception:
        log.exception("duckdb read failed; falling back to pandas")
        return _read_pandas(path)


def query_df(snapshot_id: str, sql_where: str = "", columns: Optional[List[str]] = None, limit: int = 0) -> Optional[pd.DataFrame]:
    """Small convenience for callers that want to push a filter down to DuckDB.

    `sql_where` is an SQL WHERE clause (no WHERE keyword) evaluated against
    the parquet. Use snake_case column names — matches pandas columns.
    """
    snap = get_snapshot(snapshot_id)
    if not snap:
        return None
    path = snap.parquet_path
    if not path or not os.path.exists(path):
        return None
    if path.endswith(".pkl"):
        df = _read_pandas(path)
        # pandas-side best-effort; no SQL parser here
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        if limit > 0:
            df = df.head(limit)
        return df
    try:
        duck = _get_duck()
        cols = "*" if not columns else ", ".join(f'"{c}"' for c in columns)
        where = f"WHERE {sql_where}" if sql_where else ""
        lim = f"LIMIT {int(limit)}" if limit > 0 else ""
        safe_path = path.replace("'", "''")
        sql = f"SELECT {cols} FROM read_parquet('{safe_path}') {where} {lim}"
        return duck.sql(sql).to_df()
    except Exception:
        log.exception("duckdb query failed; falling back to pandas read")
        df = _read_pandas(path)
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        if limit > 0:
            df = df.head(limit)
        return df


# ─── Active-snapshot glue for AppState ──────────────────────────────────────
def get_active_snapshot_id(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("active_snapshot_id") or None


def set_active_snapshot(settings: Dict[str, Any], snapshot_id: str) -> Optional[Snapshot]:
    snap = get_snapshot(snapshot_id)
    if not snap:
        return None
    settings["active_snapshot_id"] = snap.snapshot_id
    return snap
