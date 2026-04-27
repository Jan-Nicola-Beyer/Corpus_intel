"""Central application state — all data flows through this object.

Rules (CLAUDE.md):
  * No globals. Every endpoint reads/writes through AppState.
  * Persists to sessions/latest.ci (JSON) after every major mutation.
  * DataFrames are cached to parquet beside the session file so the JSON stays small.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from corpus_intel.constants import (
    DATASETS_DIR,
    SESSION_FILE,
    SESSION_HISTORY_DIR,
    SESSION_HISTORY_KEEP,
)

log = logging.getLogger("corpus_intel.state")


# ─── Nested types ────────────────────────────────────────────────────────────
@dataclass
class DataFrameMeta:
    """Metadata for an uploaded dataset. DataFrame itself lives on disk."""
    dataset_id: str
    original_filename: str
    uploaded_at: str                # ISO timestamp
    row_count: int
    columns: List[str]
    source_id: str                  # detected source adapter id, e.g. "brandwatch"
    source_confidence: float        # 0-1
    source_candidates: List[Dict[str, Any]] = field(default_factory=list)
    parquet_path: str = ""          # absolute path to cached DataFrame
    mapping: Dict[str, str] = field(default_factory=dict)  # source_col -> canonical_col
    mapping_source: str = "auto"    # "auto" | "ai" | "manual"
    quality_flags: Dict[str, Any] = field(default_factory=dict)
    # SHA-1 of the raw uploaded bytes. Used to detect re-uploads of the same
    # file so the user can choose replace vs. add-as-new instead of silently
    # accumulating duplicates.
    content_hash: str = ""


@dataclass
class SliceDef:
    slice_id: str
    name: str
    query: str                # Human-readable description (boolean query, or an inline sampling label)
    created_at: str
    row_count: int = 0
    # "query" slices are re-evaluated against the current corpus every time;
    # "sample" slices are frozen to a specific set of _row_idx values (reproducible);
    # "compose" slices are composed boolean queries (A AND B, A AND NOT B, etc.).
    kind: str = "query"
    spec: Dict[str, Any] = field(default_factory=dict)   # method-specific params (method, n, seed, by_col, base_query …)
    indices: List[int] = field(default_factory=list)     # frozen _row_idx for sample slices; empty for query/compose


@dataclass
class Codebook:
    """A named set of code categories. Categories are dicts with keys:
        cat_id, title, description, exclusion_group, shortcut_key, color.
    Stored as dicts (not nested dataclasses) so JSON round-trips stay trivial."""
    codebook_id: str
    name: str
    version: str
    categories: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""


@dataclass
class TopicSet:
    """One run of topic induction + classification.

    topics is a list of dicts (easy JSON round-trip) with keys:
        topic_id, name, description, keywords, example_row_indices, count
    row_assignments: row_idx (str) -> topic_id (or "other").
    """
    topic_set_id: str
    name: str
    created_at: str
    sample_size: int = 200
    target_k: int = 8          # approx. number of topics the inducer aims for
    scope_slice_id: str = ""
    goal: str = ""
    status: str = "draft"   # draft | inducing | induced | classifying | ready | error
    model: str = ""
    cost_usd: float = 0.0
    topics: List[Dict[str, Any]] = field(default_factory=list)
    row_assignments: Dict[str, str] = field(default_factory=dict)
    total_rows: int = 0        # how many rows in scope when last classified
    last_error: str = ""
    # Snapshot of the previous run taken automatically before re-induction,
    # so the user can revert a bad re-run. Keys: topics, row_assignments,
    # model, cost_usd, status, saved_at. Empty if no prior run.
    prior_run: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProvenanceEvent:
    ts: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)


# ─── Root state ──────────────────────────────────────────────────────────────
@dataclass
class AppState:
    project_name: str = ""
    datasets: Dict[str, DataFrameMeta] = field(default_factory=dict)
    corpus_rows: int = 0                       # merged corpus row count (Phase 2)
    corpus_parquet: str = ""                   # path to merged corpus parquet
    corpus_sources: List[str] = field(default_factory=list)  # dataset_ids used in the current corpus
    corpus_built_at: str = ""                  # ISO timestamp of last build
    corpus_stats: Dict[str, Any] = field(default_factory=dict)  # last MergeStats.to_dict()
    schema_mapping: Dict[str, Dict[str, str]] = field(default_factory=dict)
    slices: Dict[str, SliceDef] = field(default_factory=dict)
    codebooks: Dict[str, Codebook] = field(default_factory=dict)
    active_codebook: Optional[str] = None
    # tags: row_idx (str) -> list of {cat_id, coder, ts, source, codebook_id}
    # Each entry is a single tag placed by a single coder. Multiple coders can
    # place the same cat_id on the same row (IRR overlap).
    tags: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    ai_classifications: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Per-row free-text memos (inductive/grounded-theory notes). Keyed by row_idx
    # (str). Each entry: {memo_id, text, coder, ts, codebook_id}. A coder may add
    # multiple memos to one row; deletion is by memo_id.
    memos: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    topic_sets: Dict[str, TopicSet] = field(default_factory=dict)
    active_topic_set: Optional[str] = None
    provenance: List[ProvenanceEvent] = field(default_factory=list)
    api_key: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)

    # ─── Session-local (not persisted) ───────────────────────────────────
    # Undo stack for manual coding (50 deep).
    _undo_stack: List[Dict[str, Any]] = field(default_factory=list)
    # Redo stack — pushed-to when undo runs, cleared when a new mutation lands.
    _redo_stack: List[Dict[str, Any]] = field(default_factory=list)

    # ─── Persistence ─────────────────────────────────────────────────────
    _save_lock = threading.Lock()
    # Reentrant lock protecting compound mutations (budget RMW, corpus rebuild,
    # tag application, etc.). FastAPI runs sync endpoints in a thread pool so
    # concurrent writes are real. Use via `with get_state_lock():` — reentrant
    # so helpers that take it can be called by endpoints that already hold it.
    _mutation_lock = threading.RLock()

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready dict. api_key + undo/redo stacks are stripped from the disk payload."""
        data = asdict(self)
        data.pop("_save_lock", None)
        data.pop("_mutation_lock", None)
        data.pop("_undo_stack", None)
        data.pop("_redo_stack", None)
        data["api_key"] = ""   # never persist the key
        return data

    def save(self, path: str = SESSION_FILE) -> bool:
        """Atomic save: write to tmp, fsync, rename over latest.ci, rotate to history."""
        with self._save_lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.makedirs(SESSION_HISTORY_DIR, exist_ok=True)
                payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)
                tmp = f"{path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp, path)
                self._rotate_history(payload)
                return True
            except Exception as e:
                log.exception("Failed to save session: %s", e)
                return False

    @staticmethod
    def _rotate_history(payload: str) -> None:
        """Keep last N timestamped snapshots under sessions/history/."""
        try:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            hist = os.path.join(SESSION_HISTORY_DIR, f"latest-{ts}.ci")
            with open(hist, "w", encoding="utf-8") as f:
                f.write(payload)
            # prune
            entries = sorted(
                (e for e in os.listdir(SESSION_HISTORY_DIR) if e.startswith("latest-") and e.endswith(".ci")),
                reverse=True,
            )
            for old in entries[SESSION_HISTORY_KEEP:]:
                try:
                    os.remove(os.path.join(SESSION_HISTORY_DIR, old))
                except OSError:
                    pass
        except Exception:
            log.exception("History rotation failed")

    @classmethod
    def _try_load(cls, path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @classmethod
    def load(cls, path: str = SESSION_FILE) -> "AppState":
        raw = cls._try_load(path)
        recovery_note = ""
        if raw is None and os.path.exists(path):
            # corrupt or unreadable main file — try history
            log.warning("latest.ci unreadable; attempting history recovery")
            try:
                entries = sorted(
                    (e for e in os.listdir(SESSION_HISTORY_DIR) if e.startswith("latest-") and e.endswith(".ci")),
                    reverse=True,
                )
            except OSError:
                entries = []
            for candidate in entries:
                cand_path = os.path.join(SESSION_HISTORY_DIR, candidate)
                raw = cls._try_load(cand_path)
                if raw is not None:
                    recovery_note = candidate
                    log.warning("Recovered session from %s", candidate)
                    # quarantine the corrupt file
                    try:
                        os.replace(path, f"{path}.corrupt-{candidate}")
                    except OSError:
                        pass
                    break
        if raw is None:
            return cls()

        # Be tolerant of older session files missing newly-added fields.
        def _kw(cls_: type, v: Dict[str, Any]) -> Dict[str, Any]:
            allowed = {f_.name for f_ in cls_.__dataclass_fields__.values()}
            return {kk: vv for kk, vv in (v or {}).items() if kk in allowed}
        datasets = {k: DataFrameMeta(**_kw(DataFrameMeta, v)) for k, v in raw.get("datasets", {}).items()}
        slices = {k: SliceDef(**_kw(SliceDef, v)) for k, v in raw.get("slices", {}).items()}
        codebooks = {k: Codebook(**_kw(Codebook, v)) for k, v in raw.get("codebooks", {}).items()}
        provenance = [ProvenanceEvent(**e) for e in raw.get("provenance", [])]

        # topic_sets: tolerant to missing keys so older sessions still open.
        topic_sets: Dict[str, TopicSet] = {}
        raw_sets = raw.get("topic_sets") or {}
        for k, v in raw_sets.items():
            if not isinstance(v, dict):
                continue
            allowed = {f_.name for f_ in TopicSet.__dataclass_fields__.values()}
            v2 = {kk: vv for kk, vv in v.items() if kk in allowed}
            try:
                topic_sets[k] = TopicSet(**v2)
            except TypeError:
                continue

        # tags migration: early sessions stored bare strings; wrap them.
        raw_tags = raw.get("tags", {}) or {}
        tags: Dict[str, List[Dict[str, Any]]] = {}
        for row_key, entries in raw_tags.items():
            out: List[Dict[str, Any]] = []
            for e in entries:
                if isinstance(e, str):
                    out.append({"cat_id": e, "coder": "legacy", "ts": "", "source": "manual", "codebook_id": ""})
                elif isinstance(e, dict):
                    out.append(e)
            tags[str(row_key)] = out

        settings = raw.get("settings", {}) or {}
        if recovery_note:
            settings = dict(settings)
            settings["_recovered_from"] = recovery_note
        # restore undo tail from persistent log (session-local, see _undo_stack)
        try:
            from corpus_intel.core import undo_log
            undo_tail = undo_log.load_tail(limit=50)
        except Exception:
            undo_tail = []
        state = cls(
            project_name=raw.get("project_name", ""),
            datasets=datasets,
            corpus_rows=raw.get("corpus_rows", 0),
            corpus_parquet=raw.get("corpus_parquet", ""),
            corpus_sources=list(raw.get("corpus_sources", []) or []),
            corpus_built_at=raw.get("corpus_built_at", ""),
            corpus_stats=raw.get("corpus_stats", {}) or {},
            schema_mapping=raw.get("schema_mapping", {}),
            slices=slices,
            codebooks=codebooks,
            active_codebook=raw.get("active_codebook"),
            tags=tags,
            ai_classifications=raw.get("ai_classifications", {}),
            memos=raw.get("memos", {}) or {},
            topic_sets=topic_sets,
            active_topic_set=raw.get("active_topic_set"),
            provenance=provenance,
            settings=settings,
        )
        state._undo_stack = list(undo_tail)
        return state

    # ─── DataFrame cache helpers ─────────────────────────────────────────
    def register_dataset(self, meta: DataFrameMeta, df: pd.DataFrame) -> None:
        """Save df to parquet and record metadata."""
        parquet_path = os.path.join(DATASETS_DIR, f"{meta.dataset_id}.parquet")
        # pyarrow required by pandas; fall back to pickle if not present.
        try:
            df.to_parquet(parquet_path, index=False)
        except (ImportError, ValueError) as e:
            log.warning("Parquet write failed (%s), falling back to pickle", e)
            parquet_path = os.path.join(DATASETS_DIR, f"{meta.dataset_id}.pkl")
            df.to_pickle(parquet_path)
        meta.parquet_path = parquet_path
        self.datasets[meta.dataset_id] = meta

    def load_dataset(self, dataset_id: str) -> Optional[pd.DataFrame]:
        meta = self.datasets.get(dataset_id)
        if not meta or not meta.parquet_path or not os.path.exists(meta.parquet_path):
            return None
        if meta.parquet_path.endswith(".pkl"):
            return pd.read_pickle(meta.parquet_path)
        return pd.read_parquet(meta.parquet_path)

    def drop_dataset(self, dataset_id: str) -> bool:
        meta = self.datasets.pop(dataset_id, None)
        if not meta:
            return False
        if meta.parquet_path and os.path.exists(meta.parquet_path):
            try:
                os.remove(meta.parquet_path)
            except OSError:
                log.warning("Could not remove %s", meta.parquet_path)
        return True

    # ─── Corpus DataFrame (merged working set) ──────────────────────────
    def save_corpus(self, df: pd.DataFrame) -> str:
        """Persist the merged corpus to parquet/pkl. Returns the on-disk path."""
        path = os.path.join(DATASETS_DIR, "_corpus.parquet")
        try:
            df.to_parquet(path, index=False)
        except (ImportError, ValueError) as e:
            log.warning("Corpus parquet write failed (%s), falling back to pickle", e)
            path = os.path.join(DATASETS_DIR, "_corpus.pkl")
            df.to_pickle(path)
        self.corpus_parquet = path
        self.corpus_rows = int(len(df))
        return path

    def load_corpus(self, columns: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
        """Read the merged corpus back.

        `columns` is an optional whitelist — pandas/pyarrow will read only
        those columns from the parquet, which is 10–50× faster than loading
        all 25 columns when you only need a preview. .pkl fallback (no
        pyarrow) still loads the whole frame."""
        path = self.corpus_parquet
        if not path or not os.path.exists(path):
            return None
        if path.endswith(".pkl"):
            df = pd.read_pickle(path)
            if columns:
                keep = [c for c in columns if c in df.columns]
                return df[keep] if keep else df
            return df
        if columns:
            try:
                return pd.read_parquet(path, columns=list(columns))
            except Exception:  # unknown column → fall through to full read
                pass
        return pd.read_parquet(path)

    def corpus_row_count(self) -> int:
        """Row count without loading the corpus. Trusts the recorded metadata,
        falls back to parquet metadata if out of sync."""
        if self.corpus_rows > 0:
            return int(self.corpus_rows)
        path = self.corpus_parquet
        if not path or not os.path.exists(path):
            return 0
        try:
            if path.endswith(".parquet"):
                import pyarrow.parquet as pq  # type: ignore
                pf = pq.ParquetFile(path)
                return int(pf.metadata.num_rows)
        except Exception:
            pass
        df = self.load_corpus()
        return int(len(df)) if df is not None else 0

    def corpus_duck_query(self, sql: str, params: Optional[List[Any]] = None):
        """Run a DuckDB SQL query against the corpus parquet without loading it
        into pandas. Use for aggregations over very large corpora (> 50k rows,
        see DUCKDB_THRESHOLD_ROWS). The table name is `corpus`. Returns a
        pandas DataFrame.

        Example:
            state.corpus_duck_query("SELECT platform, COUNT(*) FROM corpus GROUP BY platform")
        """
        path = self.corpus_parquet
        if not path or not os.path.exists(path) or not path.endswith(".parquet"):
            return None
        try:
            import duckdb  # type: ignore
        except ImportError:
            log.warning("duckdb not installed; corpus_duck_query unavailable")
            return None
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{path.replace(chr(39), chr(39)+chr(39))}')")
            rel = con.execute(sql, params or [])
            return rel.fetch_df()
        finally:
            con.close()

    def clear_corpus(self) -> None:
        path = self.corpus_parquet
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                log.warning("Could not remove corpus at %s", path)
        self.corpus_parquet = ""
        self.corpus_rows = 0
        self.corpus_sources = []
        self.corpus_built_at = ""
        self.corpus_stats = {}

    @staticmethod
    def new_id(prefix: str = "ds") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ─── Module-level singleton accessor ─────────────────────────────────────────
_state_lock = threading.Lock()
_state: Optional[AppState] = None


def get_state() -> AppState:
    """Return the (lazily-loaded) process-wide AppState."""
    global _state
    if _state is None:
        with _state_lock:
            if _state is None:
                _state = AppState.load()
    return _state


def save_state() -> bool:
    return get_state().save()


def reset_state() -> AppState:
    """For tests only."""
    global _state
    with _state_lock:
        _state = AppState()
    return _state


def get_state_lock() -> "threading.RLock":
    """Return the RLock protecting mutations on the shared AppState.

    Endpoints that do compound read-modify-write work (budget spend, corpus
    rebuild, tag bulk apply, session reset) should take this lock for the full
    RMW window. Reads are allowed to race (Python dict/list reads are atomic
    enough for our read-heavy paths)."""
    return get_state()._mutation_lock
