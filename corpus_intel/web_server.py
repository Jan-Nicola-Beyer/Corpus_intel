"""Corpus Intel — FastAPI server.

Phase 0: scaffold + /api/state
Phase 1: dataset upload, list, mapping suggestions, AI fallback.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from corpus_intel.ai.claude_client import ClaudeClientError, call_claude, extract_json
from corpus_intel.ai.prompts import COLUMN_MAPPING_PROMPT, COLUMN_MAPPING_USER_TEMPLATE
from corpus_intel.app_state import DataFrameMeta, ProvenanceEvent, SliceDef, TopicSet, get_state, reset_state, save_state
from corpus_intel.constants import (
    ALLOWED_EXTENSIONS,
    APP_NAME,
    APP_VERSION,
    MAX_UPLOAD_BYTES,
    SESSION_DIR,
    STATIC_DIR,
)
from corpus_intel import claude_health
from corpus_intel.core.filters import FilterSpec, apply_filter, paginate
from corpus_intel.core.ingest import preview_rows, quality_flags, read_upload
from corpus_intel.core.merge import build_corpus, corpus_facets
from corpus_intel.core.schema import (
    CANONICAL_FIELDS,
    CANONICAL_NAMES,
    REQUIRED_CANONICAL,
    suggest_mapping,
    validate_mapping,
)
from corpus_intel.core.codebook import (
    CodebookError,
    add_category,
    export_dict as codebook_export_dict,
    import_dict as codebook_import_dict,
    new_codebook,
    remove_category,
    reorder_categories,
    shortcut_conflicts,
    starter_hate_speech,
    update_category,
    validate as validate_codebook,
)
from corpus_intel.core.coding import (
    CodingError,
    all_coders,
    bulk_tag,
    progress as coding_progress,
    row_tags,
    tag_row,
    undo_last,
    untag_row,
)
from corpus_intel.core.slicer import (
    SlicerError,
    SampleSpec,
    apply_boolean_query,
    describe_sample,
    equal_chunks,
    indices_of,
    run_sample,
    SYNTAX_HELP,
)
from corpus_intel.core.stats_engine import (
    cohens_kappa_per_category,
    coder_overlap,
    compare_descriptives,
    crosstab as stats_crosstab,
    descriptives as stats_descriptives,
    krippendorffs_alpha_per_category,
    list_analytic_columns,
    ngrams as stats_ngrams,
    timeseries as stats_timeseries,
)
from corpus_intel.core.sources import detect_all, get_adapter, list_adapters
from corpus_intel.core import answer_cache as ai_cache
from corpus_intel.core.ai_coding import (
    DEFAULT_BATCH_SIZE,
    preflight as ai_preflight,
    run_classification,
    suggest_codebook as ai_suggest_codebook,
)
from corpus_intel.core import topics as topics_core
from corpus_intel.core.topics import (
    DEFAULT_CLASSIFY_BATCH,
    DEFAULT_SAMPLE_SIZE,
    TopicError,
)
from corpus_intel.core import provenance as provenance_mod
from corpus_intel.core import ai_health as _ai_health

log = logging.getLogger("corpus_intel.web")

app = FastAPI(title=APP_NAME, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PACKAGE_DIR)


@app.on_event("startup")
def _startup_health_check() -> None:
    claude_health.run_startup_check(
        app_name="Corpus Intel",
        app_root=_PACKAGE_DIR,
        required_imports=["anthropic", "fastapi", "pandas", "pydantic"],
        required_paths=[SESSION_DIR],
        requirements_file=os.path.join(_REPO_ROOT, "requirements.txt"),
    )


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(claude_health.health_report())


# ─── Index ──────────────────────────────────────────────────────────────────
@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/static/intro.html")


# ─── State ──────────────────────────────────────────────────────────────────
def _dataset_payload(meta: DataFrameMeta) -> Dict[str, Any]:
    d = asdict(meta)
    d.pop("parquet_path", None)
    return d


@app.get("/api/state")
def get_state_endpoint() -> JSONResponse:
    s = get_state()
    # Summarise coding without dumping every tag — frontend asks per-row as needed.
    cb_id = s.active_codebook or ""
    rows_tagged = sum(
        1 for entries in s.tags.values()
        if any((not cb_id) or e.get("codebook_id") == cb_id for e in entries)
    )
    coder_names: Dict[str, int] = {}
    for entries in s.tags.values():
        seen_local = set()
        for e in entries:
            if cb_id and e.get("codebook_id") != cb_id:
                continue
            c = e.get("coder") or ""
            if c and c not in seen_local:
                coder_names[c] = coder_names.get(c, 0) + 1
                seen_local.add(c)

    payload = {
        "phase": 6,
        "project_name": s.project_name,
        "datasets": {k: _dataset_payload(v) for k, v in s.datasets.items()},
        "corpus": {
            "rows": s.corpus_rows,
            "built_at": s.corpus_built_at,
            "source_dataset_ids": s.corpus_sources,
            "stats": s.corpus_stats,
            "built": bool(s.corpus_parquet and s.corpus_rows),
        },
        "slices": {k: _slice_payload(v, s.slices) for k, v in s.slices.items()},
        "codebooks": {k: asdict(v) for k, v in s.codebooks.items()},
        "active_codebook": s.active_codebook,
        "coding": {
            "coder_name": (s.settings.get("coder_name") or "").strip(),
            "coding_slice_id": (s.settings.get("coding_slice_id") or ""),
            "rows_tagged": rows_tagged,
            "coders": coder_names,
            "undo_available": len(s._undo_stack),
        },
        "topic_sets": {k: asdict(v) for k, v in s.topic_sets.items()},
        "active_topic_set": s.active_topic_set,
        "settings": s.settings,
        "has_api_key": bool(s.api_key),
        "ai_health": _ai_health.to_dict(),
        "canonical_schema": [asdict(f) for f in CANONICAL_FIELDS],
        "canonical_required": REQUIRED_CANONICAL,
        "adapters": list_adapters(),
        "ready": True,
    }
    return JSONResponse(payload)


class ApiKeyRequest(BaseModel):
    api_key: str


@app.post("/api/settings/api_key")
def set_api_key(body: ApiKeyRequest) -> JSONResponse:
    s = get_state()
    s.api_key = (body.api_key or "").strip()
    # api_key is intentionally not persisted (see AppState.to_dict)
    save_state()
    return JSONResponse({"ok": True, "has_api_key": bool(s.api_key)})


@app.post("/api/session/reset")
def session_reset() -> JSONResponse:
    """Fully reset the in-memory AppState and delete persisted session files.
    Destructive — everything (uploaded datasets, corpus, codebooks, slices,
    topics, logs) is wiped. API key and coder name are cleared too. Caller is
    responsible for confirming with the user."""
    import shutil
    from corpus_intel.constants import (
        SESSION_DIR, SESSION_FILE, SNAPSHOTS_FILE, UNDO_LOG_FILE,
        DATASETS_DIR, CORPORA_DIR, CACHE_DIR,
    )
    from corpus_intel.app_state import get_state_lock
    # Extra append-only JSONL registries that used to survive reset and leaked
    # state from a prior session into the new one (span annotations, journey
    # log, decisions registry). Wipe them alongside the JSON state file.
    _extra_files = [
        os.path.join(SESSION_DIR, "span_annotations.jsonl"),
        os.path.join(SESSION_DIR, "journey_log.jsonl"),
        os.path.join(SESSION_DIR, "decisions_registry.jsonl"),
    ]
    with get_state_lock():
        reset_state()
        for path in (SESSION_FILE, SNAPSHOTS_FILE, UNDO_LOG_FILE, *_extra_files):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                log.warning("could not remove %s: %s", path, e)
        for folder in (DATASETS_DIR, CORPORA_DIR, CACHE_DIR):
            try:
                if os.path.isdir(folder):
                    shutil.rmtree(folder)
                    os.makedirs(folder, exist_ok=True)
            except OSError as e:
                log.warning("could not reset %s: %s", folder, e)
        # Drop the in-process answer-cache so the next classification doesn't
        # hit stale in-memory entries after we've wiped the JSON file.
        try:
            from corpus_intel.core import answer_cache as _ac
            _ac.reload_from_disk()
        except Exception:
            pass
        save_state()
    return JSONResponse({"ok": True})


# ─── Datasets ───────────────────────────────────────────────────────────────
@app.post("/api/datasets/upload_async")
async def upload_dataset_async(file: UploadFile = File(...)) -> JSONResponse:
    """Spool the upload to disk in chunks (cap ≈200 MB in RAM at any moment) and
    parse on a background job so the request returns quickly. Frontend tracks
    progress via /api/jobs/{id}/events."""
    import shutil
    import tempfile

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported extension: {ext}")

    # Stream straight to a temp file — never hold the whole blob in RAM.
    tmp = tempfile.NamedTemporaryFile(prefix="ci-upload-", suffix=ext, delete=False)
    tmp_path = tmp.name
    bytes_written = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)  # 1 MB
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > MAX_UPLOAD_BYTES:
                tmp.close()
                try: os.remove(tmp_path)
                except OSError: pass
                raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_BYTES} bytes")
            tmp.write(chunk)
        tmp.flush()
    finally:
        tmp.close()
    if bytes_written == 0:
        try: os.remove(tmp_path)
        except OSError: pass
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    filename = file.filename
    from corpus_intel.core import jobs as _jobs

    def _run(job):
        try:
            job.publish(progress_pct=5, message="reading from disk…")
            with open(tmp_path, "rb") as f:
                blob = f.read()
            import hashlib as _hashlib
            content_hash = _hashlib.sha1(blob).hexdigest()
            # Async variant can't return a 409 to the client — surface the
            # duplicate via the job result instead, letting the frontend warn.
            s = get_state()
            existing = [m for m in s.datasets.values() if getattr(m, "content_hash", "") == content_hash]
            if existing:
                m = existing[0]
                job.publish(progress_pct=100, message=f"duplicate of {m.original_filename}")
                return {
                    "duplicate_of": m.dataset_id,
                    "duplicate_filename": m.original_filename,
                    "message": "Byte-identical file already uploaded; skipped. Delete the old dataset first or rename this file.",
                }
            job.publish(progress_pct=25, message="parsing rows…")
            result = read_upload(filename, blob)
            if result.df.empty:
                raise ValueError("File parsed but produced zero rows")
            job.publish(progress_pct=55, message="detecting source…")
            candidates = detect_all(result.df, filename)
            top = candidates[0]
            source_id = top["id"]
            confidence = top["confidence"]
            adapter = get_adapter(source_id)
            auto_mapping = suggest_mapping(list(result.df.columns), pre_wired=adapter.CANONICAL_COLUMNS if adapter else None)
            best_by_target: Dict[str, tuple] = {}
            for src, info in auto_mapping.items():
                canon = info.get("canonical")
                if not canon: continue
                conf = float(info.get("confidence") or 0.0)
                prev = best_by_target.get(canon)
                if prev is None or conf > prev[1]:
                    best_by_target[canon] = (src, conf)
            mapping = {src: canon for canon, (src, _) in best_by_target.items()}
            flags = quality_flags(result.df, mapping)
            job.publish(progress_pct=85, message="registering dataset…")
            dataset_id = s.new_id("ds")
            meta = DataFrameMeta(
                dataset_id=dataset_id, original_filename=filename,
                uploaded_at=dt.datetime.now().isoformat(timespec="seconds"),
                row_count=int(len(result.df)), columns=list(result.df.columns),
                source_id=source_id, source_confidence=confidence, source_candidates=candidates,
                mapping=mapping, mapping_source="auto", quality_flags=flags,
                content_hash=content_hash,
            )
            s.register_dataset(meta, result.df)
            s.provenance.append(ProvenanceEvent(
                ts=meta.uploaded_at, action="upload",
                params={"dataset_id": dataset_id, "filename": filename, "rows": meta.row_count,
                        "source_id": source_id, "confidence": confidence},
            ))
            save_state()
            job.publish(progress_pct=100, message=f"done — {meta.row_count} rows")
            return {"dataset_id": dataset_id, "rows": meta.row_count, "source_id": source_id}
        finally:
            try: os.remove(tmp_path)
            except OSError: pass

    j = _jobs.submit("upload", _run, params={"filename": filename, "bytes": bytes_written})
    return JSONResponse({"job_id": j.id, "bytes": bytes_written})


@app.post("/api/datasets/upload")
async def upload_dataset(file: UploadFile = File(...), replace_hash_match: bool = False) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported extension: {ext}")

    blob = await file.read()
    if len(blob) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_BYTES} bytes")

    import hashlib as _hashlib
    content_hash = _hashlib.sha1(blob).hexdigest()
    s_pre = get_state()
    existing = [m for m in s_pre.datasets.values() if getattr(m, "content_hash", "") == content_hash]
    if existing and not replace_hash_match:
        m = existing[0]
        return JSONResponse(
            {
                "ok": False,
                "blocked": "duplicate_content_hash",
                "message": f"This file is byte-identical to an existing dataset ('{m.original_filename}', {m.row_count} rows, uploaded {m.uploaded_at}). Retry with replace_hash_match=true to delete the old one and register this as a replacement, or upload a different file.",
                "duplicate_of": {
                    "dataset_id": m.dataset_id,
                    "original_filename": m.original_filename,
                    "row_count": m.row_count,
                    "uploaded_at": m.uploaded_at,
                },
            },
            status_code=409,
        )
    if existing and replace_hash_match:
        # Drop all byte-identical predecessors; the new upload takes their place.
        for m in existing:
            if m.dataset_id in (s_pre.corpus_sources or []):
                s_pre.clear_corpus()
            s_pre.drop_dataset(m.dataset_id)

    try:
        result = read_upload(file.filename, blob)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("ingest failed")
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")

    if result.df.empty:
        raise HTTPException(status_code=400, detail="File parsed but produced zero rows")

    candidates = detect_all(result.df, file.filename)
    top = candidates[0]
    source_id = top["id"]
    confidence = top["confidence"]
    # If every adapter scored low, keep the best but flag it — AI fallback available.
    if confidence < 0.6:
        log.info("low-confidence detection (best=%s @ %.2f)", source_id, confidence)

    adapter = get_adapter(source_id)
    auto_mapping = suggest_mapping(list(result.df.columns), pre_wired=adapter.CANONICAL_COLUMNS if adapter else None)
    # Reduce to source→canonical dict, deduping by canonical target:
    # when multiple source columns propose the same canonical field, keep the
    # one with the highest confidence (ties broken by order-of-appearance).
    best_by_target: Dict[str, tuple] = {}
    for src, info in auto_mapping.items():
        canon = info.get("canonical")
        if not canon:
            continue
        conf = float(info.get("confidence") or 0.0)
        prev = best_by_target.get(canon)
        if prev is None or conf > prev[1]:
            best_by_target[canon] = (src, conf)
    mapping: Dict[str, str] = {src: canon for canon, (src, _) in best_by_target.items()}
    flags = quality_flags(result.df, mapping)

    s = get_state()
    dataset_id = s.new_id("ds")
    meta = DataFrameMeta(
        dataset_id=dataset_id,
        original_filename=file.filename,
        uploaded_at=dt.datetime.now().isoformat(timespec="seconds"),
        row_count=int(len(result.df)),
        columns=list(result.df.columns),
        source_id=source_id,
        source_confidence=confidence,
        source_candidates=candidates,
        mapping=mapping,
        mapping_source="auto",
        quality_flags=flags,
        content_hash=content_hash,
    )
    s.register_dataset(meta, result.df)
    s.provenance.append(
        ProvenanceEvent(
            ts=meta.uploaded_at,
            action="upload",
            params={
                "dataset_id": dataset_id,
                "filename": file.filename,
                "rows": meta.row_count,
                "source_id": source_id,
                "confidence": confidence,
            },
        )
    )
    save_state()

    return JSONResponse(
        {
            "dataset_id": dataset_id,
            "meta": _dataset_payload(meta),
            "suggestions": auto_mapping,
            "preview": preview_rows(result.df, 5),
            "encoding": result.encoding,
            "delimiter": result.delimiter,
            "notes": result.notes,
        }
    )


@app.get("/api/datasets")
def list_datasets() -> JSONResponse:
    s = get_state()
    return JSONResponse({k: _dataset_payload(v) for k, v in s.datasets.items()})


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str) -> JSONResponse:
    s = get_state()
    meta = s.datasets.get(dataset_id)
    if not meta:
        raise HTTPException(404, "dataset not found")
    adapter = get_adapter(meta.source_id)
    suggestions = suggest_mapping(meta.columns, pre_wired=adapter.CANONICAL_COLUMNS if adapter else None)
    df = s.load_dataset(dataset_id)
    preview = preview_rows(df, 5) if df is not None else []
    return JSONResponse(
        {
            "meta": _dataset_payload(meta),
            "suggestions": suggestions,
            "preview": preview,
        }
    )


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, force: bool = False) -> JSONResponse:
    s = get_state()
    if dataset_id not in s.datasets:
        raise HTTPException(404, "dataset not found")
    # Deleting a dataset that is still part of the active corpus leaves the
    # corpus with orphaned source rows (no backing metadata). Refuse unless
    # the caller confirms with ?force=true, in which case we drop the corpus
    # and any snapshots referencing the dataset.
    in_corpus = dataset_id in (s.corpus_sources or [])
    if in_corpus and not force:
        return JSONResponse(
            {
                "ok": False,
                "blocked": "dataset_in_active_corpus",
                "message": "This dataset is part of the current corpus. Rebuild the corpus without it, or retry with force=true to also clear the corpus.",
                "hint": "DELETE /api/datasets/{id}?force=true",
            },
            status_code=409,
        )
    if in_corpus and force:
        s.clear_corpus()
        s.settings.pop("active_snapshot_id", None)
        log.info("force-deleted dataset %s while in corpus; corpus cleared", dataset_id)
    # Any snapshot that references the dataset is now a zombie — re-activating
    # it would resurrect rows whose source metadata is gone. Purge them on
    # force delete so the snapshot list stays trustworthy.
    purged_snapshots: List[str] = []
    if force:
        from corpus_intel.core import corpus_store as _cs
        purged_snapshots = _cs.purge_snapshots_referencing(dataset_id)
    if not s.drop_dataset(dataset_id):
        raise HTTPException(404, "dataset not found")
    if purged_snapshots:
        s.provenance.append(
            ProvenanceEvent(
                ts=dt.datetime.now().isoformat(timespec="seconds"),
                action="snapshots_purge",
                params={"dataset_id": dataset_id, "snapshot_ids": purged_snapshots},
            )
        )
    save_state()
    return JSONResponse({
        "ok": True,
        "corpus_cleared": bool(in_corpus and force),
        "purged_snapshots": purged_snapshots,
    })


class MappingUpdate(BaseModel):
    mapping: Dict[str, Optional[str]]
    source_id: Optional[str] = None


@app.post("/api/datasets/{dataset_id}/mapping")
def update_mapping(dataset_id: str, body: MappingUpdate) -> JSONResponse:
    s = get_state()
    meta = s.datasets.get(dataset_id)
    if not meta:
        raise HTTPException(404, "dataset not found")

    clean = {k: v for k, v in body.mapping.items() if v and v in CANONICAL_NAMES}
    ok, problems = validate_mapping(clean)

    meta.mapping = clean
    meta.mapping_source = "manual"
    if body.source_id and get_adapter(body.source_id):
        meta.source_id = body.source_id

    df = s.load_dataset(dataset_id)
    if df is not None:
        meta.quality_flags = quality_flags(df, clean)

    s.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="mapping_update",
            params={"dataset_id": dataset_id, "source_id": meta.source_id, "fields": len(clean)},
        )
    )
    save_state()
    return JSONResponse({"ok": ok, "problems": problems, "meta": _dataset_payload(meta)})


@app.post("/api/datasets/{dataset_id}/mapping/suggest")
def ai_suggest_mapping(dataset_id: str) -> JSONResponse:
    """Ask Claude Haiku to propose a mapping. Falls back to the deterministic
    column-name heuristic if no API key is configured."""
    s = get_state()
    meta = s.datasets.get(dataset_id)
    if not meta:
        raise HTTPException(404, "dataset not found")

    df = s.load_dataset(dataset_id)
    if df is None:
        raise HTTPException(500, "dataset dataframe missing on disk")

    if not s.api_key:
        # Deterministic fallback: re-run the same adapter-aware heuristic the
        # sync upload path uses. Caller still gets a useful suggestion plus a
        # note flagging the fallback so the UI can prompt for an API key.
        adapter = get_adapter(meta.source_id)
        suggestions = suggest_mapping(
            list(df.columns),
            pre_wired=adapter.CANONICAL_COLUMNS if adapter else None,
        )
        detail: Dict[str, Dict[str, Any]] = {}
        for src, info in suggestions.items():
            detail[src] = {
                "canonical": info.get("canonical"),
                "confidence": float(info.get("confidence") or 0.0),
                "reason": info.get("reason", "") or "heuristic match",
                "method": "heuristic",
            }
        # Don't overwrite meta.mapping — this is a preview, not a commit.
        return JSONResponse({
            "meta": _dataset_payload(meta),
            "suggestions": detail,
            "fallback": "heuristic",
            "note": "No API key configured — returned deterministic heuristic suggestions. Add an API key in Settings for richer AI suggestions.",
        })

    # Build column→examples payload
    samples: Dict[str, List[Any]] = {}
    head = df.head(5)
    for col in meta.columns:
        vals = [v for v in head[col].tolist() if v is not None]
        samples[col] = [str(v)[:160] for v in vals]

    user_msg = COLUMN_MAPPING_USER_TEMPLATE.format(payload=json.dumps(samples, ensure_ascii=False, default=str))
    try:
        raw = call_claude(
            system=COLUMN_MAPPING_PROMPT,
            user_msg=user_msg,
            task="mapping",
            api_key=s.api_key,
            max_tokens=1200,
        )
    except ClaudeClientError as e:
        raise HTTPException(502, f"Claude call failed: {e}")

    parsed = extract_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("mapping"), list):
        raise HTTPException(502, "AI response could not be parsed as mapping JSON")

    ai_mapping: Dict[str, Optional[str]] = {}
    detail: Dict[str, Dict[str, Any]] = {}
    for entry in parsed["mapping"]:
        src = entry.get("source")
        canon = entry.get("canonical")
        if canon not in CANONICAL_NAMES:
            canon = None
        if src:
            ai_mapping[src] = canon
            detail[src] = {
                "canonical": canon,
                "confidence": float(entry.get("confidence") or 0.0),
                "reason": entry.get("reason", ""),
                "method": "ai",
            }

    # Persist as the new mapping (only non-null targets).
    meta.mapping = {k: v for k, v in ai_mapping.items() if v}
    meta.mapping_source = "ai"
    meta.quality_flags = quality_flags(df, meta.mapping)

    s.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="mapping_ai_suggest",
            params={"dataset_id": dataset_id, "fields": len(meta.mapping)},
        )
    )
    save_state()
    return JSONResponse({"meta": _dataset_payload(meta), "suggestions": detail})


# ─── Corpus (Phase 2) ───────────────────────────────────────────────────────
# Columns shown in the Corpus table view (smaller than the 22-field schema).
CORPUS_PREVIEW_COLUMNS: List[str] = [
    "created_at", "platform", "source_dataset", "author_handle",
    "text", "language", "country", "like_count", "share_count",
    "comment_count", "view_count", "url",
]


def _corpus_row_to_dict(row: "pd.Series") -> Dict[str, Any]:  # type: ignore[name-defined]
    import pandas as pd  # local import keeps this helper self-contained
    out: Dict[str, Any] = {}
    for k, v in row.items():
        # pd.isna() covers None, NaN, NaT, and pd.NA in one check.
        try:
            if pd.isna(v):
                out[k] = None
                continue
        except (TypeError, ValueError):
            pass  # non-scalar (list/array) — fall through
        try:
            # Datetimes → ISO; everything else → str/number as-is.
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            else:
                out[k] = v if isinstance(v, (int, float, bool, str)) else str(v)
        except Exception:
            out[k] = str(v)
    return out


def _corpus_preview(df: "pd.DataFrame", n: int = 50, offset: int = 0) -> List[Dict[str, Any]]:  # type: ignore[name-defined]
    import pandas as pd  # local import to keep top-level deps tidy
    if df is None or df.empty:
        return []
    cols = [c for c in CORPUS_PREVIEW_COLUMNS if c in df.columns]
    if "_row_idx" in df.columns:
        cols = ["_row_idx"] + cols
    sub = df.iloc[offset:offset + n][cols]
    return [_corpus_row_to_dict(r) for _, r in sub.iterrows()]


class MergeRequest(BaseModel):
    dataset_ids: List[str]
    dedupe_near_text: bool = True
    force: bool = False  # proceed even if some datasets have incomplete mappings


@app.post("/api/corpus/merge")
def corpus_merge(body: MergeRequest) -> JSONResponse:
    s = get_state()
    if not body.dataset_ids:
        raise HTTPException(400, "Pick at least one dataset to build a corpus.")
    # Gate on mapping validity: a dataset without post_id / text / created_at
    # will contribute rows that can't be deduped, tagged, or sorted. Historically
    # merge silently accepted these; that masked user errors. Block unless force.
    invalid: List[Dict[str, Any]] = []
    for ds_id in body.dataset_ids:
        meta = s.datasets.get(ds_id)
        if not meta:
            raise HTTPException(400, f"Dataset '{ds_id}' not found.")
        ok, problems = validate_mapping(meta.mapping or {})
        if not ok:
            invalid.append({
                "dataset_id": ds_id,
                "filename": meta.original_filename,
                "problems": problems,
            })
    if invalid and not body.force:
        return JSONResponse(
            {
                "ok": False,
                "blocked": "incomplete_mappings",
                "message": "Some selected datasets are missing required column mappings (post_id, text, created_at). Fix mappings or retry with force=true.",
                "invalid": invalid,
            },
            status_code=409,
        )
    try:
        df, stats = build_corpus(s, body.dataset_ids, dedupe_near_text=body.dedupe_near_text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("corpus merge failed")
        raise HTTPException(500, f"Corpus build failed: {e}")

    # Serialize the corpus/state mutation so a concurrent slicer / tag write
    # cannot observe a half-rebuilt corpus (parquet swapped but slices still
    # carrying the old _row_idx). Slice invalidation lives inside the lock.
    from corpus_intel.app_state import get_state_lock
    with get_state_lock():
        s.save_corpus(df)
        s.corpus_sources = list(body.dataset_ids)
        s.corpus_built_at = dt.datetime.now().isoformat(timespec="seconds")
        s.corpus_stats = stats.to_dict()
        # Frozen "sample" slices point at _row_idx values from the previous
        # corpus. After a rebuild, those indices refer to different rows.
        # Mark them stale by clearing indices and tagging the spec — UI can
        # warn the user. Query/compose slices re-evaluate against the new
        # corpus on every call, so they stay valid automatically.
        for sl in s.slices.values():
            if sl.kind == "sample" and sl.indices:
                sl.spec = dict(sl.spec or {})
                sl.spec["invalidated_at"] = s.corpus_built_at
                sl.spec["invalidated_reason"] = "corpus rebuilt; frozen indices no longer reference the same rows"
                sl.indices = []
                sl.row_count = 0
        # Register immutable snapshot (P10.2) — content-addressed, survives rebuilds.
        from corpus_intel.core import corpus_store as _cs
        snap = _cs.create_snapshot(
            df,
            name=f"build-{s.corpus_built_at}",
            source_dataset_ids=list(body.dataset_ids),
            dedup_params={"dedupe_near_text": bool(body.dedupe_near_text)},
            stats=stats.to_dict(),
        )
        s.settings["active_snapshot_id"] = snap.snapshot_id
        s.provenance.append(
            ProvenanceEvent(
                ts=s.corpus_built_at,
                action="corpus_merge",
                params={
                    "dataset_ids": list(body.dataset_ids),
                    "dedupe_near_text": body.dedupe_near_text,
                    "snapshot_id": snap.snapshot_id,
                    "stats": stats.to_dict(),
                },
            )
        )
        save_state()

    return JSONResponse({
        "rows": int(len(df)),
        "stats": stats.to_dict(),
        "facets": corpus_facets(df),
        "preview": _corpus_preview(df, 50, 0),
        "preview_columns": [c for c in CORPUS_PREVIEW_COLUMNS if c in df.columns],
        "built_at": s.corpus_built_at,
        "snapshot_id": snap.snapshot_id,
    })


@app.get("/api/corpus")
def corpus_summary() -> JSONResponse:
    s = get_state()
    # For large corpora read only the preview columns from parquet (projection
    # pushdown) — 20–50× faster first paint than reading the full 25-column
    # frame. Facets still need a broader slice, so load them from a separate
    # projection. Below the DuckDB threshold just do one full read.
    from corpus_intel.constants import DUCKDB_THRESHOLD_ROWS
    row_total = s.corpus_row_count()
    if not row_total:
        return JSONResponse({
            "built": False,
            "rows": 0,
            "source_dataset_ids": [],
            "stats": {},
            "built_at": "",
        })
    if row_total > DUCKDB_THRESHOLD_ROWS:
        preview_cols = ["_row_idx"] + CORPUS_PREVIEW_COLUMNS
        preview_df = s.load_corpus(columns=preview_cols)
        facet_cols = list({"platform", "language", "source_dataset", "source_id", "country", "created_at"})
        facet_df = s.load_corpus(columns=facet_cols)
        if preview_df is None or facet_df is None:
            return JSONResponse({
                "built": False,
                "rows": 0,
                "source_dataset_ids": [],
                "stats": {},
                "built_at": "",
            })
        return JSONResponse({
            "built": True,
            "rows": int(row_total),
            "source_dataset_ids": s.corpus_sources,
            "stats": s.corpus_stats,
            "built_at": s.corpus_built_at,
            "facets": corpus_facets(facet_df),
            "preview": _corpus_preview(preview_df, 50, 0),
            "preview_columns": [c for c in CORPUS_PREVIEW_COLUMNS if c in preview_df.columns],
            "large_corpus": True,
        })
    df = s.load_corpus()
    if df is None:
        return JSONResponse({
            "built": False,
            "rows": 0,
            "source_dataset_ids": [],
            "stats": {},
            "built_at": "",
        })
    return JSONResponse({
        "built": True,
        "rows": int(len(df)),
        "source_dataset_ids": s.corpus_sources,
        "stats": s.corpus_stats,
        "built_at": s.corpus_built_at,
        "facets": corpus_facets(df),
        "preview": _corpus_preview(df, 50, 0),
        "preview_columns": [c for c in CORPUS_PREVIEW_COLUMNS if c in df.columns],
    })


@app.delete("/api/corpus")
def corpus_clear() -> JSONResponse:
    # Clearing the corpus orphans any frozen-sample slice whose _row_idx values
    # reference the (now absent) corpus. Mirror the invalidation pattern used by
    # merge / snapshot-activate so stale sample slices are flagged for the user.
    from corpus_intel.app_state import get_state_lock
    s = get_state()
    invalidated_slices: List[str] = []
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    with get_state_lock():
        s.clear_corpus()
        s.settings.pop("active_snapshot_id", None)
        for sl in s.slices.values():
            if sl.kind == "sample" and sl.indices:
                sl.spec = dict(sl.spec or {})
                sl.spec["invalidated_at"] = now_iso
                sl.spec["invalidated_reason"] = "corpus cleared; frozen indices no longer reference any rows"
                sl.indices = []
                sl.row_count = 0
                invalidated_slices.append(sl.slice_id)
        s.provenance.append(
            ProvenanceEvent(
                ts=now_iso,
                action="corpus_clear",
                params={"invalidated_slices": invalidated_slices},
            )
        )
        save_state()
    return JSONResponse({"ok": True, "invalidated_slices": invalidated_slices})


class FilterRequest(BaseModel):
    text: str = ""
    regex: bool = False
    case_sensitive: bool = False
    platforms: List[str] = []
    languages: List[str] = []
    source_ids: List[str] = []
    source_datasets: List[str] = []
    countries: List[str] = []
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    engagement: Dict[str, Dict[str, Optional[float]]] = {}
    sort_by: Optional[str] = None
    sort_desc: bool = True
    page: int = 1
    page_size: int = 50


SORTABLE_COLUMNS = {
    "_row_idx", "created_at", "platform", "source_id", "source_dataset",
    "author_handle", "author_name", "language", "country", "region",
    "like_count", "share_count", "comment_count", "view_count",
}


def _apply_sort(df: "pd.DataFrame", sort_by: Optional[str], sort_desc: bool) -> "pd.DataFrame":
    if not sort_by or sort_by not in df.columns or sort_by not in SORTABLE_COLUMNS:
        return df
    if sort_by in ("like_count", "share_count", "comment_count", "view_count"):
        key = pd.to_numeric(df[sort_by], errors="coerce")
    else:
        key = df[sort_by]
    order = key.sort_values(ascending=not sort_desc, na_position="last", kind="mergesort").index
    return df.loc[order]


@app.post("/api/corpus/filter")
def corpus_filter(body: FilterRequest) -> JSONResponse:
    s = get_state()
    df = s.load_corpus()
    if df is None:
        raise HTTPException(400, "No corpus built yet. Build one from the Corpus tab.")
    spec = FilterSpec.from_dict(body.model_dump(exclude={"page", "page_size", "sort_by", "sort_desc"}))
    try:
        filtered = apply_filter(df, spec)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sorted_df = _apply_sort(filtered, body.sort_by, body.sort_desc)
    page_df = paginate(sorted_df, body.page, body.page_size)
    return JSONResponse({
        "rows_in_corpus": int(len(df)),
        "rows_filtered": int(len(filtered)),
        "page": max(1, int(body.page or 1)),
        "page_size": max(1, min(500, int(body.page_size or 50))),
        "sort_by": body.sort_by or None,
        "sort_desc": bool(body.sort_desc),
        "preview": [_corpus_row_to_dict(r) for _, r in page_df.iterrows()],
        "preview_columns": [c for c in (["_row_idx"] + CORPUS_PREVIEW_COLUMNS) if c in filtered.columns],
    })


def _content_disposition(filename: str, default: str = "export.bin") -> str:
    """Build an RFC 5987-compatible Content-Disposition header value.

    Keeps non-ASCII characters (Spanish, German, CJK, Arabic, …) in the
    filename via the `filename*` parameter, while falling back to an ASCII-
    safe `filename=` for older clients. Replaces only path-separators and
    control chars in the ASCII fallback — never strips Unicode letters.
    """
    import urllib.parse
    raw = filename or default
    # Strip path separators and control chars; keep unicode letters/digits.
    cleaned = "".join(
        ch for ch in raw
        if ch not in ('\\', '/', '\0', '\r', '\n') and ord(ch) >= 0x20
    ).strip() or default
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned).strip("_") or default
    # Percent-encode the full UTF-8 name for filename*.
    encoded = urllib.parse.quote(cleaned, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _export_columns(df: "pd.DataFrame") -> List[str]:
    """Columns to include in CSV export — row_idx + canonical preview + url if present."""
    extras = ["author_id", "author_name", "post_id", "hashtags", "region", "url"]
    ordered = ["_row_idx"] + CORPUS_PREVIEW_COLUMNS + [c for c in extras if c not in CORPUS_PREVIEW_COLUMNS]
    seen = set()
    out: List[str] = []
    for c in ordered:
        if c in df.columns and c not in seen:
            out.append(c)
            seen.add(c)
    return out


def _csv_stream(df: "pd.DataFrame", filename: str) -> StreamingResponse:
    import io
    cols = _export_columns(df)
    sub = df[cols] if cols else df
    buf = io.StringIO()
    # BOM so Excel on Windows auto-detects UTF-8 for non-ASCII text.
    buf.write("\ufeff")
    sub.to_csv(buf, index=False, encoding="utf-8")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(filename, "corpus.csv")},
    )


@app.post("/api/corpus/export.csv")
def corpus_export_csv(body: FilterRequest) -> StreamingResponse:
    s = get_state()
    df = s.load_corpus()
    if df is None:
        raise HTTPException(400, "No corpus built yet.")
    spec = FilterSpec.from_dict(body.model_dump(exclude={"page", "page_size", "sort_by", "sort_desc"}))
    try:
        filtered = apply_filter(df, spec)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sorted_df = _apply_sort(filtered, body.sort_by, body.sort_desc)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return _csv_stream(sorted_df, f"corpus-{ts}-{len(sorted_df)}rows.csv")


@app.get("/api/corpus/row/{row_idx}")
def corpus_row(row_idx: int) -> JSONResponse:
    s = get_state()
    df = s.load_corpus()
    if df is None:
        raise HTTPException(404, "No corpus built yet.")
    if row_idx < 0 or row_idx >= len(df):
        raise HTTPException(404, "row not found")
    row = df.iloc[row_idx]
    return JSONResponse({"row": _corpus_row_to_dict(row)})


# ─── Slicer (Phase 3) ───────────────────────────────────────────────────────
def _slice_payload(sd: SliceDef, all_slices: Optional[Dict[str, "SliceDef"]] = None) -> Dict[str, Any]:
    d = asdict(sd)
    # The on-disk indices list can be huge; the frontend only needs its length.
    d["indices_count"] = len(d.get("indices") or [])
    d.pop("indices", None)
    # Lift invalidation flags from spec to top-level so the frontend can show
    # a warning badge without spelunking inside spec.
    spec = d.get("spec") or {}
    if spec.get("invalidated_at"):
        d["invalidated_at"] = spec["invalidated_at"]
        d["invalidated_reason"] = spec.get("invalidated_reason", "")
    # For composed slices, check that both parent slices still exist; a
    # dangling parent means re-eval will silently return empty.
    if sd.kind == "compose" or spec.get("method") == "compose_frozen":
        a_id = spec.get("a_id")
        b_id = spec.get("b_id")
        if all_slices is not None and (a_id or b_id):
            missing = [pid for pid in (a_id, b_id) if pid and pid not in all_slices]
            if missing:
                d["parent_missing"] = missing
                d["dangling"] = True
    return d


def _require_corpus() -> "pd.DataFrame":  # type: ignore[name-defined]
    s = get_state()
    df = s.load_corpus()
    if df is None:
        raise HTTPException(400, "No corpus built yet. Build one from the Corpus tab first.")
    return df


def _run_query_mask(df: "pd.DataFrame", query: str) -> "pd.Series":  # type: ignore[name-defined]
    if "text" not in df.columns:
        raise HTTPException(400, "Corpus has no 'text' column to query.")
    try:
        return apply_boolean_query(df, query)
    except SlicerError as e:
        raise HTTPException(400, f"Query error: {e}")


def _evaluate_slice(df: "pd.DataFrame", sd: SliceDef) -> "pd.DataFrame":  # type: ignore[name-defined]
    """Produce the subset of ``df`` that a given SliceDef represents.

    - kind="sample": intersect with frozen _row_idx list (immutable, even if corpus changes).
    - kind="query" or "compose": re-run the boolean query against the current corpus.
    """
    if sd.kind == "sample" and sd.indices:
        if "_row_idx" not in df.columns:
            return df.iloc[0:0]
        return df[df["_row_idx"].isin(sd.indices)]
    # query / compose: re-evaluate
    mask = _run_query_mask(df, sd.query)
    return df[mask]


def _slice_name_key(name: str) -> str:
    """Unicode-aware key for slice-name equality checks.

    .lower() isn't sufficient for non-ASCII casing (e.g. German ß / Turkish
    dotless i); NFKC normalizes compatibility equivalents before casefolding
    so visually identical names collide as expected.
    """
    import unicodedata
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _unique_slice_name(s, proposed: str) -> None:
    """Raise 409 if a slice with the same (Unicode-normalized) name already exists."""
    proposed = proposed.strip()
    if not proposed:
        raise HTTPException(400, "Slice needs a name.")
    key = _slice_name_key(proposed)
    for sd in s.slices.values():
        if _slice_name_key(sd.name) == key:
            raise HTTPException(409, f"A slice named {proposed!r} already exists.")


def _numeric_columns(df: "pd.DataFrame") -> List[str]:  # type: ignore[name-defined]
    import pandas as pd
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and c != "_row_idx"]


def _categorical_columns(df: "pd.DataFrame") -> List[str]:  # type: ignore[name-defined]
    """Reasonable stratification candidates: low-cardinality string columns."""
    import pandas as pd
    out: List[str] = []
    for c in df.columns:
        if c in ("_row_idx", "text", "url", "post_id", "author_id", "author_handle", "author_name"):
            continue
        if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c]):
            continue
        nunique = df[c].nunique(dropna=True)
        if 2 <= nunique <= 50:
            out.append(c)
    return out


class SlicePreviewRequest(BaseModel):
    query: str
    page: int = 1
    page_size: int = 25


@app.post("/api/slices/preview")
def slice_preview(body: SlicePreviewRequest) -> JSONResponse:
    """Run a boolean query and return counts + a paged preview, without saving."""
    df = _require_corpus()
    mask = _run_query_mask(df, body.query)
    sub = df[mask]
    page_df = paginate(sub, body.page, body.page_size)
    return JSONResponse({
        "rows_in_corpus": int(len(df)),
        "rows_matched": int(len(sub)),
        "page": max(1, int(body.page or 1)),
        "page_size": max(1, min(500, int(body.page_size or 25))),
        "preview": [_corpus_row_to_dict(r) for _, r in page_df.iterrows()],
        "preview_columns": [c for c in (["_row_idx"] + CORPUS_PREVIEW_COLUMNS) if c in sub.columns],
    })


class SliceFromIndicesRequest(BaseModel):
    name: str
    indices: List[int]
    note: str = ""


@app.post("/api/slices/from_indices")
def slice_from_indices(body: SliceFromIndicesRequest) -> JSONResponse:
    """Save a frozen slice from an explicit _row_idx list. Used by topic drill-in."""
    s = get_state()
    df = _require_corpus()
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Slice name required.")
    _unique_slice_name(s, name)
    n = int(s.corpus_rows or len(df))
    clean = sorted({int(i) for i in (body.indices or []) if 0 <= int(i) < n})
    if not clean:
        raise HTTPException(400, "No valid row indices supplied.")
    slice_id = s.new_id("sl")
    sd = SliceDef(
        slice_id=slice_id,
        name=name,
        query=(body.note or f"{len(clean)} frozen rows"),
        created_at=dt.datetime.now().isoformat(timespec="seconds"),
        row_count=len(clean),
        kind="sample",
        spec={"method": "indices", "source": body.note or ""},
        indices=clean,
    )
    s.slices[slice_id] = sd
    s.provenance.append(
        ProvenanceEvent(
            ts=sd.created_at,
            action="slice_from_indices",
            params={"slice_id": slice_id, "name": name, "rows": len(clean), "note": body.note},
        )
    )
    save_state()
    return JSONResponse({"slice": _slice_payload(sd, s.slices)})


class SliceSaveRequest(BaseModel):
    name: str
    query: str


@app.post("/api/slices")
def slice_save(body: SliceSaveRequest) -> JSONResponse:
    """Save a named boolean-query slice. Name must be unique (case-insensitive)."""
    s = get_state()
    df = _require_corpus()
    name = (body.name or "").strip()
    _unique_slice_name(s, name)
    mask = _run_query_mask(df, body.query)
    row_count = int(mask.sum())
    slice_id = s.new_id("sl")
    sd = SliceDef(
        slice_id=slice_id,
        name=name,
        query=body.query,
        created_at=dt.datetime.now().isoformat(timespec="seconds"),
        row_count=row_count,
        kind="query",
    )
    s.slices[slice_id] = sd
    s.provenance.append(
        ProvenanceEvent(
            ts=sd.created_at,
            action="slice_save",
            params={"slice_id": slice_id, "name": name, "kind": "query", "rows": row_count},
        )
    )
    save_state()
    return JSONResponse({"slice": _slice_payload(sd, s.slices)})


@app.get("/api/slices")
def slices_list() -> JSONResponse:
    s = get_state()
    return JSONResponse({k: _slice_payload(v, s.slices) for k, v in s.slices.items()})


@app.get("/api/slices/syntax")
def slicer_syntax() -> JSONResponse:
    return JSONResponse({"help": SYNTAX_HELP})


@app.get("/api/slices/{slice_id}")
def slice_detail(slice_id: str, page: int = 1, page_size: int = 25) -> JSONResponse:
    """Fetch a paginated preview of a saved slice (any kind), against the current corpus."""
    s = get_state()
    sd = s.slices.get(slice_id)
    if not sd:
        raise HTTPException(404, "slice not found")
    df = _require_corpus()
    sub = _evaluate_slice(df, sd)
    page_df = paginate(sub, page, page_size)
    if int(len(sub)) != sd.row_count:
        sd.row_count = int(len(sub))
        save_state()
    return JSONResponse({
        "slice": _slice_payload(sd, s.slices),
        "rows_in_corpus": int(len(df)),
        "rows_matched": int(len(sub)),
        "page": max(1, int(page or 1)),
        "page_size": max(1, min(500, int(page_size or 25))),
        "preview": [_corpus_row_to_dict(r) for _, r in page_df.iterrows()],
        "preview_columns": [c for c in (["_row_idx"] + CORPUS_PREVIEW_COLUMNS) if c in sub.columns],
    })


@app.get("/api/slices/{slice_id}/export.csv")
def slice_export_csv(slice_id: str) -> StreamingResponse:
    s = get_state()
    sd = s.slices.get(slice_id)
    if not sd:
        raise HTTPException(404, "slice not found")
    df = _require_corpus()
    sub = _evaluate_slice(df, sd)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return _csv_stream(sub, f"slice-{sd.name}-{ts}-{len(sub)}rows.csv")


@app.delete("/api/slices/{slice_id}")
def slice_delete(slice_id: str, purge_tags: bool = False) -> JSONResponse:
    """Delete a slice. Tags on rows are keyed by row_idx, not slice, so by
    default they survive. Pass ?purge_tags=true to also remove tags and AI
    classifications on rows uniquely belonging to this slice (i.e. not part
    of any other saved slice's indices/query result)."""
    s = get_state()
    sd = s.slices.pop(slice_id, None)
    if not sd:
        raise HTTPException(404, "slice not found")

    purged_rows = 0
    purged_tags = 0
    purged_ai = 0
    if purge_tags:
        row_ids: List[int] = []
        if sd.kind == "sample" and sd.indices:
            row_ids = [int(r) for r in sd.indices]
        else:
            df = s.load_corpus()
            if df is not None and "_row_idx" in df.columns:
                try:
                    sub = _evaluate_slice(df, sd)
                    row_ids = [int(r) for r in sub["_row_idx"].tolist()]
                except HTTPException:
                    row_ids = []
        if row_ids:
            # Rows that also belong to another surviving slice should keep
            # their tags — only purge rows unique to the deleted slice.
            other_rows: set = set()
            for other in s.slices.values():
                if other.slice_id == slice_id:
                    continue
                if other.kind == "sample" and other.indices:
                    other_rows.update(int(r) for r in other.indices)
            unique_rows = [r for r in row_ids if r not in other_rows]
            for r in unique_rows:
                key = str(r)
                if key in s.tags:
                    purged_tags += len(s.tags[key])
                    del s.tags[key]
                    purged_rows += 1
                if key in s.ai_classifications:
                    del s.ai_classifications[key]
                    purged_ai += 1

    s.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="slice_delete",
            params={
                "slice_id": slice_id, "name": sd.name,
                "purge_tags": bool(purge_tags),
                "purged_rows": purged_rows,
                "purged_tag_entries": purged_tags,
                "purged_ai_classifications": purged_ai,
            },
        )
    )
    save_state()
    return JSONResponse({
        "ok": True,
        "purged_rows": purged_rows,
        "purged_tag_entries": purged_tags,
        "purged_ai_classifications": purged_ai,
    })


class SliceDiffRequest(BaseModel):
    slice_a_id: str
    slice_b_id: str


def _slice_mask(df: "pd.DataFrame", sd: SliceDef) -> "pd.Series":  # type: ignore[name-defined]
    import pandas as pd
    if sd.kind == "sample" and sd.indices:
        if "_row_idx" not in df.columns:
            return pd.Series(False, index=df.index)
        return df["_row_idx"].isin(sd.indices)
    return _run_query_mask(df, sd.query)


@app.post("/api/slices/diff")
def slice_diff(body: SliceDiffRequest) -> JSONResponse:
    """Return set-operation counts (A∩B, A only, B only, union) for two saved slices."""
    s = get_state()
    a = s.slices.get(body.slice_a_id)
    b = s.slices.get(body.slice_b_id)
    if not a or not b:
        raise HTTPException(404, "one of the slices could not be found")
    df = _require_corpus()
    mask_a = _slice_mask(df, a)
    mask_b = _slice_mask(df, b)
    return JSONResponse({
        "rows_in_corpus": int(len(df)),
        "a": {"id": a.slice_id, "name": a.name, "rows": int(mask_a.sum())},
        "b": {"id": b.slice_id, "name": b.name, "rows": int(mask_b.sum())},
        "both": int((mask_a & mask_b).sum()),
        "a_only": int((mask_a & ~mask_b).sum()),
        "b_only": int((~mask_a & mask_b).sum()),
        "either": int((mask_a | mask_b).sum()),
    })


# ─── Sampling & splitting ───────────────────────────────────────────────────
@app.get("/api/slices/sample/columns")
def slice_sample_columns() -> JSONResponse:
    """Return the candidate columns for stratification (categorical) and top-N (numeric)."""
    s = get_state()
    df = s.load_corpus()
    if df is None:
        return JSONResponse({"categorical": [], "numeric": [], "corpus_rows": 0})
    return JSONResponse({
        "categorical": _categorical_columns(df),
        "numeric": _numeric_columns(df),
        "corpus_rows": int(len(df)),
    })


class SampleRequest(BaseModel):
    method: str = "random"
    n: int = 200
    seed: int = 42
    by_col: Optional[str] = None
    ascending: bool = False
    step: int = 10
    base_query: str = ""
    dedupe_text: bool = False
    page: int = 1
    page_size: int = 25


@app.post("/api/slices/sample/preview")
def slice_sample_preview(body: SampleRequest) -> JSONResponse:
    """Run a sampling spec and return counts + a paged preview, without saving."""
    df = _require_corpus()
    spec = SampleSpec.from_dict(body.model_dump())
    try:
        sampled = run_sample(df, spec)
    except SlicerError as e:
        raise HTTPException(400, str(e))
    page_df = paginate(sampled, body.page, body.page_size)
    return JSONResponse({
        "rows_in_corpus": int(len(df)),
        "rows_sampled": int(len(sampled)),
        "page": max(1, int(body.page or 1)),
        "page_size": max(1, min(500, int(body.page_size or 25))),
        "description": describe_sample(spec),
        "preview": [_corpus_row_to_dict(r) for _, r in page_df.iterrows()],
        "preview_columns": [c for c in (["_row_idx"] + CORPUS_PREVIEW_COLUMNS) if c in sampled.columns],
    })


class SampleSaveRequest(SampleRequest):
    name: str = ""


@app.post("/api/slices/sample")
def slice_sample_save(body: SampleSaveRequest) -> JSONResponse:
    """Freeze a sample to disk as a named slice. The _row_idx list is persisted so
    the slice is reproducible even if the corpus is rebuilt."""
    s = get_state()
    df = _require_corpus()
    _unique_slice_name(s, body.name)
    spec = SampleSpec.from_dict(body.model_dump())
    try:
        sampled = run_sample(df, spec)
    except SlicerError as e:
        raise HTTPException(400, str(e))
    indices = indices_of(sampled)
    slice_id = s.new_id("sl")
    sd = SliceDef(
        slice_id=slice_id,
        name=body.name.strip(),
        query=describe_sample(spec),
        created_at=dt.datetime.now().isoformat(timespec="seconds"),
        row_count=int(len(sampled)),
        kind="sample",
        spec=asdict(spec),
        indices=indices,
    )
    s.slices[slice_id] = sd
    s.provenance.append(
        ProvenanceEvent(
            ts=sd.created_at,
            action="slice_sample",
            params={"slice_id": slice_id, "name": sd.name, "method": spec.method, "rows": sd.row_count},
        )
    )
    save_state()
    return JSONResponse({"slice": _slice_payload(sd, s.slices)})


class SplitRequest(BaseModel):
    name_prefix: str
    k: int = 2
    seed: int = 42
    overlap_pct: float = 0.0
    base_query: str = ""


@app.post("/api/slices/split")
def slice_split(body: SplitRequest) -> JSONResponse:
    """Split the corpus (optionally pre-filtered) into K equal chunks, saving each
    as its own sample-kind slice. Useful for parallel coding or train/test."""
    s = get_state()
    df = _require_corpus()
    k = int(body.k or 2)
    if k < 2 or k > 20:
        raise HTTPException(400, "k must be between 2 and 20.")
    prefix = (body.name_prefix or "").strip()
    if not prefix:
        raise HTTPException(400, "Pick a name prefix (e.g. 'batch').")
    # Make sure none of the generated names collide.
    for i in range(1, k + 1):
        _unique_slice_name(s, f"{prefix} — chunk {i} of {k}")

    # Apply optional base query
    try:
        if body.base_query.strip():
            mask = _run_query_mask(df, body.base_query)
            base_df = df[mask]
        else:
            base_df = df
    except HTTPException:
        raise

    chunks = equal_chunks(base_df, k, seed=int(body.seed or 42), overlap_pct=float(body.overlap_pct or 0.0))
    saved: List[Dict[str, Any]] = []
    now = dt.datetime.now().isoformat(timespec="seconds")
    for i, chunk_df in enumerate(chunks, start=1):
        slice_id = s.new_id("sl")
        label_bits = [f"Equal-chunk split ({i}/{k}) · seed={body.seed}"]
        if body.overlap_pct > 0:
            label_bits.append(f"overlap={body.overlap_pct}%")
        if body.base_query.strip():
            label_bits.append(f"filtered by: {body.base_query.strip()}")
        sd = SliceDef(
            slice_id=slice_id,
            name=f"{prefix} — chunk {i} of {k}",
            query=" · ".join(label_bits),
            created_at=now,
            row_count=int(len(chunk_df)),
            kind="sample",
            spec={"method": "equal_chunks", "k": k, "index": i, "seed": int(body.seed),
                  "overlap_pct": float(body.overlap_pct), "base_query": body.base_query},
            indices=indices_of(chunk_df),
        )
        s.slices[slice_id] = sd
        saved.append(_slice_payload(sd, s.slices))
    s.provenance.append(
        ProvenanceEvent(
            ts=now,
            action="slice_split",
            params={"prefix": prefix, "k": k, "overlap_pct": body.overlap_pct,
                    "seed": body.seed, "base_query": body.base_query},
        )
    )
    save_state()
    return JSONResponse({"slices": saved})


class ComposeRequest(BaseModel):
    slice_a_id: str
    slice_b_id: str
    op: str               # "and", "or", "and_not", "or_not"
    name: str


@app.post("/api/slices/compose")
def slice_compose(body: ComposeRequest) -> JSONResponse:
    """Save a new slice built from two existing ones by set operation.

    If both inputs are query-kind, the new slice is also query-kind with a
    composed boolean string — so it auto-updates when the corpus changes.
    If either input is a frozen sample, the composition is frozen too.
    """
    s = get_state()
    a = s.slices.get(body.slice_a_id)
    b = s.slices.get(body.slice_b_id)
    if not a or not b:
        raise HTTPException(404, "one of the slices could not be found")
    op = (body.op or "").lower()
    if op not in ("and", "or", "and_not", "or_not"):
        raise HTTPException(400, "op must be one of: and, or, and_not, or_not")
    _unique_slice_name(s, body.name)
    df = _require_corpus()

    both_query = a.kind in ("query", "compose") and b.kind in ("query", "compose")
    slice_id = s.new_id("sl")
    now = dt.datetime.now().isoformat(timespec="seconds")

    if both_query:
        # Compose a boolean string and let it re-evaluate on every read.
        glue = {"and": "AND", "or": "OR", "and_not": "AND NOT", "or_not": "OR NOT"}[op]
        composed = f"({a.query}) {glue} ({b.query})"
        mask = _run_query_mask(df, composed)
        sd = SliceDef(
            slice_id=slice_id,
            name=body.name.strip(),
            query=composed,
            created_at=now,
            row_count=int(mask.sum()),
            kind="compose",
            spec={"op": op, "a_id": a.slice_id, "b_id": b.slice_id, "a_name": a.name, "b_name": b.name},
        )
    else:
        # At least one frozen input → freeze the result too.
        mask_a = _slice_mask(df, a)
        mask_b = _slice_mask(df, b)
        if op == "and":        mask = mask_a & mask_b
        elif op == "or":       mask = mask_a | mask_b
        elif op == "and_not":  mask = mask_a & ~mask_b
        else:                  mask = mask_a | ~mask_b
        sub = df[mask]
        label = f'"{a.name}" {op.upper()} "{b.name}"'
        sd = SliceDef(
            slice_id=slice_id,
            name=body.name.strip(),
            query=label,
            created_at=now,
            row_count=int(len(sub)),
            kind="sample",
            spec={"method": "compose_frozen", "op": op, "a_id": a.slice_id, "b_id": b.slice_id,
                  "a_name": a.name, "b_name": b.name},
            indices=indices_of(sub),
        )
    s.slices[slice_id] = sd
    s.provenance.append(
        ProvenanceEvent(
            ts=now,
            action="slice_compose",
            params={"slice_id": slice_id, "name": sd.name, "op": op,
                    "a": a.slice_id, "b": b.slice_id, "kind": sd.kind},
        )
    )
    save_state()
    return JSONResponse({"slice": _slice_payload(sd, s.slices)})


# ─── Settings: coder identity ───────────────────────────────────────────────
class CoderRequest(BaseModel):
    coder_name: str


@app.post("/api/settings/coder")
def set_coder(body: CoderRequest) -> JSONResponse:
    s = get_state()
    name = (body.coder_name or "").strip()
    s.settings["coder_name"] = name
    save_state()
    return JSONResponse({"ok": True, "coder_name": name})


class GoalRequest(BaseModel):
    goal: str = ""


_VALID_GOALS = {"", "build", "code", "explore"}


@app.post("/api/settings/goal")
def set_goal(body: GoalRequest) -> JSONResponse:
    """Store the user's current project goal. Used on the Home page to pick
    which next-step copy to show. Anything outside {build, code, explore} is
    stored as empty."""
    s = get_state()
    goal = (body.goal or "").strip().lower()
    if goal not in _VALID_GOALS:
        goal = ""
    s.settings["current_goal"] = goal
    save_state()
    return JSONResponse({"ok": True, "current_goal": goal})


class BudgetRequest(BaseModel):
    monthly_budget_usd: float


@app.post("/api/settings/budget")
def set_budget(body: BudgetRequest) -> JSONResponse:
    """Set the monthly AI-spend ceiling in USD. 0 disables the ceiling."""
    s = get_state()
    v = max(0.0, float(body.monthly_budget_usd or 0))
    s.settings["monthly_budget_usd"] = round(v, 4)
    save_state()
    from corpus_intel.core import budget as _b
    return JSONResponse({
        "ok": True,
        "budget_usd": _b.get_budget(s.settings),
        "spent_usd": _b.get_spent(s.settings),
    })


@app.get("/api/settings/budget")
def get_budget_status() -> JSONResponse:
    s = get_state()
    from corpus_intel.core import budget as _b
    return JSONResponse({
        "budget_usd": _b.get_budget(s.settings),
        "spent_usd": _b.get_spent(s.settings),
        "month": _b._ym(),
        "history": s.settings.get("monthly_spent") or {},
    })


@app.post("/api/session/ack_recovery")
def ack_recovery() -> JSONResponse:
    """Dismiss the 'session recovered from history' banner."""
    s = get_state()
    s.settings.pop("_recovered_from", None)
    save_state()
    return JSONResponse({"ok": True})


@app.get("/api/corpus/snapshots")
def snapshots_list() -> JSONResponse:
    """List all immutable corpus snapshots + mark the active one.

    Snapshots may reference dataset ids that were deleted since the snapshot
    was created — activating one of those resurrects rows with no backing
    metadata. Surface the gap so the UI can mark them as degraded.
    """
    from corpus_intel.core import corpus_store as _cs
    s = get_state()
    active = _cs.get_active_snapshot_id(s.settings) or ""
    known_ds = set(s.datasets.keys())
    items: List[Dict[str, Any]] = []
    for snap in _cs.list_snapshots():
        d = dict(asdict_s(snap), active=(snap.snapshot_id == active))
        src_ids = list(d.get("source_dataset_ids") or [])
        missing = [ds_id for ds_id in src_ids if ds_id not in known_ds]
        if missing:
            d["missing_sources"] = missing
            d["degraded"] = True
        items.append(d)
    return JSONResponse({"active_snapshot_id": active, "items": items})


def asdict_s(snap) -> Dict[str, Any]:
    return snap.to_dict()


class SnapshotActivateRequest(BaseModel):
    snapshot_id: str


@app.post("/api/corpus/snapshots/activate")
def snapshots_activate(body: SnapshotActivateRequest) -> JSONResponse:
    """Switch the active corpus to a previously-built snapshot. Reloads the
    on-disk parquet as the current working corpus so existing readers
    (slicer, analytics, coding) see it immediately. Sample slices are
    invalidated because their frozen _row_idx values may now point to
    different rows or be out of bounds for the activated snapshot."""
    from corpus_intel.core import corpus_store as _cs
    from corpus_intel.app_state import get_state_lock
    s = get_state()
    snap = _cs.get_snapshot(body.snapshot_id)
    if not snap:
        raise HTTPException(404, f"No snapshot '{body.snapshot_id}'.")
    # Short-circuit when the requested snapshot is already active. Skipping the
    # reload avoids needlessly invalidating sample slices whose frozen indices
    # are still valid against the unchanged parquet.
    if _cs.get_active_snapshot_id(s.settings) == snap.snapshot_id and s.corpus_rows:
        return JSONResponse({
            "ok": True,
            "active_snapshot_id": snap.snapshot_id,
            "rows": snap.rows,
            "invalidated_slices": [],
            "no_op": True,
        })
    df = _cs.open_df(snap.snapshot_id)
    if df is None:
        raise HTTPException(500, "Snapshot parquet is missing on disk.")
    invalidated_slices: List[str] = []
    with get_state_lock():
        s.save_corpus(df)
        s.corpus_sources = list(snap.source_dataset_ids or [])
        s.corpus_stats = dict(snap.stats or {})
        s.corpus_built_at = snap.created_at
        s.settings["active_snapshot_id"] = snap.snapshot_id
        now_iso = dt.datetime.now().isoformat(timespec="seconds")
        for sl in s.slices.values():
            if sl.kind == "sample" and sl.indices:
                sl.spec = dict(sl.spec or {})
                sl.spec["invalidated_at"] = now_iso
                sl.spec["invalidated_reason"] = f"snapshot '{snap.name}' activated; frozen indices may point to different rows"
                sl.indices = []
                sl.row_count = 0
                invalidated_slices.append(sl.slice_id)
        s.provenance.append(ProvenanceEvent(
            ts=now_iso,
            action="snapshot_activate",
            params={
                "snapshot_id": snap.snapshot_id, "name": snap.name, "rows": snap.rows,
                "invalidated_slices": invalidated_slices,
            },
        ))
        save_state()
    return JSONResponse({
        "ok": True,
        "active_snapshot_id": snap.snapshot_id,
        "rows": snap.rows,
        "invalidated_slices": invalidated_slices,
    })


@app.delete("/api/corpus/snapshots/{snapshot_id}")
def snapshots_delete(snapshot_id: str) -> JSONResponse:
    from corpus_intel.core import corpus_store as _cs
    s = get_state()
    active = _cs.get_active_snapshot_id(s.settings)
    if active == snapshot_id:
        raise HTTPException(400, "Cannot delete the active snapshot. Activate a different one first.")
    if not _cs.delete_snapshot(snapshot_id):
        raise HTTPException(404, f"No snapshot '{snapshot_id}'.")
    s.provenance.append(ProvenanceEvent(
        ts=dt.datetime.now().isoformat(timespec="seconds"),
        action="snapshot_delete",
        params={"snapshot_id": snapshot_id},
    ))
    save_state()
    return JSONResponse({"ok": True, "deleted": snapshot_id})


@app.get("/api/ai/health")
def ai_health_endpoint() -> JSONResponse:
    """Probe reachability of api.anthropic.com (cached 30s) plus key presence.
    The frontend pill should show OK only when reachable AND has_api_key."""
    from corpus_intel.core import ai_health
    s = get_state()
    has_key = bool(s.api_key)
    payload = ai_health.to_dict()
    payload["has_api_key"] = has_key
    payload["ready"] = bool(payload.get("reachable")) and has_key
    if payload.get("reachable") and not has_key:
        payload["reason"] = "no_api_key"
    return JSONResponse(payload)


# ─── Background jobs (P10.3) ────────────────────────────────────────────────
@app.get("/api/jobs")
def jobs_list() -> JSONResponse:
    from corpus_intel.core import jobs as _jobs
    return JSONResponse({"items": _jobs.list_jobs(limit=50)})


@app.get("/api/jobs/{job_id}")
def jobs_get(job_id: str) -> JSONResponse:
    from corpus_intel.core import jobs as _jobs
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, f"No job '{job_id}'.")
    return JSONResponse(j.to_dict())


@app.post("/api/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str) -> JSONResponse:
    from corpus_intel.core import jobs as _jobs
    ok = _jobs.request_cancel(job_id)
    if not ok:
        raise HTTPException(400, "Job not cancellable.")
    return JSONResponse({"ok": True})


@app.post("/api/jobs/clear")
def jobs_clear() -> JSONResponse:
    """Remove all finished (done/error/cancelled) jobs from the registry."""
    from corpus_intel.core import jobs as _jobs
    n = _jobs.clear_finished()
    return JSONResponse({"ok": True, "cleared": n})


@app.get("/api/jobs/{job_id}/events")
def jobs_events(job_id: str) -> StreamingResponse:
    from corpus_intel.core import jobs as _jobs
    if not _jobs.get(job_id):
        raise HTTPException(404, f"No job '{job_id}'.")
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(_jobs.sse_events(job_id), media_type="text/event-stream", headers=headers)


class MergeJobRequest(BaseModel):
    dataset_ids: List[str]
    dedupe_near_text: bool = True


@app.post("/api/corpus/merge_async")
def corpus_merge_async(body: MergeJobRequest) -> JSONResponse:
    """Kick off the merge on a background job and return the job id immediately."""
    from corpus_intel.core import jobs as _jobs
    s = get_state()
    if not body.dataset_ids:
        raise HTTPException(400, "Pick at least one dataset to build a corpus.")

    dataset_ids = list(body.dataset_ids)
    dedupe_near = bool(body.dedupe_near_text)

    def _run(job):
        job.publish(progress_pct=1, message="loading datasets…")
        df, stats = build_corpus(s, dataset_ids, dedupe_near_text=dedupe_near)
        job.publish(progress_pct=70, message="saving corpus…")
        s.save_corpus(df)
        s.corpus_sources = dataset_ids
        s.corpus_built_at = dt.datetime.now().isoformat(timespec="seconds")
        s.corpus_stats = stats.to_dict()
        from corpus_intel.core import corpus_store as _cs
        snap = _cs.create_snapshot(
            df,
            name=f"build-{s.corpus_built_at}",
            source_dataset_ids=dataset_ids,
            dedup_params={"dedupe_near_text": dedupe_near},
            stats=stats.to_dict(),
        )
        s.settings["active_snapshot_id"] = snap.snapshot_id
        s.provenance.append(
            ProvenanceEvent(
                ts=s.corpus_built_at, action="corpus_merge",
                params={"dataset_ids": dataset_ids, "dedupe_near_text": dedupe_near, "snapshot_id": snap.snapshot_id, "stats": stats.to_dict()},
            )
        )
        save_state()
        job.publish(progress_pct=100, message=f"done — {len(df)} rows")
        return {"rows": int(len(df)), "snapshot_id": snap.snapshot_id, "stats": stats.to_dict()}

    j = _jobs.submit("corpus_merge", _run, params={"dataset_ids": dataset_ids, "dedupe_near_text": dedupe_near})
    return JSONResponse({"job_id": j.id})


# ─── P11: Research rigor ────────────────────────────────────────────────────
@app.post("/api/codebooks/{codebook_id}/versions/snapshot")
def codebook_version_snapshot(codebook_id: str, note: str = "") -> JSONResponse:
    from corpus_intel.core import codebook_versions as _cv
    s = get_state()
    cb = s.codebooks.get(codebook_id)
    if not cb:
        raise HTTPException(404, f"No codebook '{codebook_id}'.")
    entry = _cv.snapshot(cb, note=note)
    return JSONResponse({"ok": True, "index": entry["index"], "ts": entry["ts"]})


@app.get("/api/codebooks/{codebook_id}/versions")
def codebook_versions_list(codebook_id: str) -> JSONResponse:
    from corpus_intel.core import codebook_versions as _cv
    versions = _cv.list_versions(codebook_id)
    return JSONResponse({"items": [{"index": v["index"], "ts": v["ts"], "note": v.get("note", ""),
                                    "category_count": len(v["payload"].get("categories") or [])} for v in versions]})


@app.get("/api/codebooks/{codebook_id}/versions/diff")
def codebook_version_diff(codebook_id: str, a: int, b: int) -> JSONResponse:
    from corpus_intel.core import codebook_versions as _cv
    return JSONResponse(_cv.diff(codebook_id, a, b))


@app.get("/api/codebooks/{codebook_id}/versions/{index}")
def codebook_version_detail(codebook_id: str, index: int) -> JSONResponse:
    from corpus_intel.core import codebook_versions as _cv
    v = _cv.get_version(codebook_id, index)
    if not v:
        raise HTTPException(404, "Version not found.")
    return JSONResponse(v)


@app.get("/api/coding/uncertain")
def coding_uncertain(limit: int = 50) -> JSONResponse:
    """Return the most-uncertain AI-coded rows for active-learning review."""
    from corpus_intel.core import active_learning as _al
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        return JSONResponse({"items": [], "reason": "corpus empty"})
    items = _al.suggest_batch(df, batch_size=int(max(1, min(500, limit))))
    return JSONResponse({"items": items, "count": len(items)})


class DiagnoseSampleRequest(BaseModel):
    sample_row_hashes: List[str] = []


@app.post("/api/coding/sample_diagnose")
def coding_sample_diagnose(body: DiagnoseSampleRequest) -> JSONResponse:
    from corpus_intel.core import sample_diagnostics as _sd
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        raise HTTPException(400, "No corpus built.")
    if not body.sample_row_hashes:
        raise HTTPException(400, "Pass `sample_row_hashes` (list of strings).")
    mask = df.get("row_hash", pd.Series(dtype=str)).astype(str).isin(set(body.sample_row_hashes))
    sample = df[mask] if "row_hash" in df.columns else df.head(0)
    report = _sd.diagnose(sample, df)
    return JSONResponse(report)


@app.get("/api/coding/irr/conflicts")
def coding_irr_conflicts() -> JSONResponse:
    """Scan coded rows for coder disagreements and return a conflict list."""
    from corpus_intel.core import irr_conflicts as _irr
    s = get_state()
    df = getattr(s, "codings_df", None)
    if df is None or (hasattr(df, "empty") and df.empty):
        return JSONResponse({"items": [], "summary": {"count": 0}})
    conflicts = _irr.find_conflicts(df)
    return JSONResponse({"items": conflicts, "summary": _irr.summarize_conflicts(conflicts)})


class AdjudicationRequest(BaseModel):
    row_id: str
    final_cat: str
    adjudicator: str = ""
    note: str = ""


@app.post("/api/coding/irr/adjudicate")
def coding_irr_adjudicate(body: AdjudicationRequest) -> JSONResponse:
    from corpus_intel.core import irr_conflicts as _irr
    entry = _irr.adjudicate(body.row_id, body.final_cat, adjudicator=body.adjudicator, note=body.note)
    return JSONResponse({"ok": True, "entry": entry})


class SpanAnnotationRequest(BaseModel):
    row_id: str
    start: int
    end: int
    cat_id: str
    coder: str = ""
    note: str = ""


@app.post("/api/coding/spans")
def coding_spans_add(body: SpanAnnotationRequest) -> JSONResponse:
    from corpus_intel.core import annotation_spans as _spans
    try:
        entry = _spans.add(body.row_id, body.start, body.end, body.cat_id, coder=body.coder, note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "entry": entry})


@app.get("/api/coding/spans/{row_id}")
def coding_spans_for_row(row_id: str) -> JSONResponse:
    from corpus_intel.core import annotation_spans as _spans
    return JSONResponse({"items": _spans.list_for_row(row_id)})


@app.delete("/api/coding/spans")
def coding_spans_remove(row_id: str, start: int, end: int, cat_id: str) -> JSONResponse:
    from corpus_intel.core import annotation_spans as _spans
    ok = _spans.remove(row_id, start, end, cat_id)
    return JSONResponse({"ok": ok})


# ─── P12: Analytical breadth ────────────────────────────────────────────────
@app.post("/api/search/build_index")
def search_build_index() -> JSONResponse:
    """Build / rebuild the TF-IDF index for the active snapshot."""
    from corpus_intel.core import semantic_search as _sem, corpus_store as _cs
    s = get_state()
    snap_id = s.settings.get("active_snapshot_id") or ""
    df = s.load_corpus()
    if df is None or df.empty:
        raise HTTPException(400, "No corpus built.")
    if not snap_id:
        snap_id = "_transient"
    try:
        idx = _sem.build_index(snap_id, df)
    except Exception as e:
        raise HTTPException(500, f"Index build failed: {e}")
    return JSONResponse({"ok": True, "snapshot_id": snap_id, "rows_indexed": len(idx.row_ids)})


class SearchRequest(BaseModel):
    query: str
    k: int = 20


@app.post("/api/search/query")
def search_query(body: SearchRequest) -> JSONResponse:
    from corpus_intel.core import semantic_search as _sem
    s = get_state()
    snap_id = s.settings.get("active_snapshot_id") or "_transient"
    try:
        hits = _sem.search(snap_id, body.query, k=int(body.k))
    except RuntimeError:
        raise HTTPException(400, "Build the search index first: POST /api/search/build_index")
    return JSONResponse({"hits": hits, "snapshot_id": snap_id})


@app.get("/api/search/scatter")
def search_scatter(method: str = "auto", sample_size: int = 5000) -> JSONResponse:
    from corpus_intel.core import umap_scatter as _us
    s = get_state()
    snap_id = s.settings.get("active_snapshot_id") or "_transient"
    try:
        pts = _us.project(snap_id, method=method, sample_size=int(sample_size))
    except RuntimeError as e:
        return JSONResponse({"points": [], "snapshot_id": snap_id, "count": 0, "note": str(e)})
    return JSONResponse({"points": pts, "snapshot_id": snap_id, "count": len(pts)})


class EnrichmentRequest(BaseModel):
    kind: str  # "sentiment" | "entities" | "stance"
    target: str = ""  # only for stance
    sample_size: int = 0  # 0 = all


@app.post("/api/enrichment/run")
def enrichment_run(body: EnrichmentRequest) -> JSONResponse:
    """Run an enrichment pass. Uses Claude Haiku via the unified client + answer cache."""
    from corpus_intel.core import enrichment as _enr
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        raise HTTPException(400, "No corpus built.")
    if body.sample_size and body.sample_size > 0:
        df = df.head(int(body.sample_size))
    api_key = s.api_key or ""
    try:
        if body.kind == "sentiment":
            res = _enr.enrich_sentiment(df, api_key=api_key)
        elif body.kind == "entities":
            res = _enr.enrich_entities(df, api_key=api_key)
        elif body.kind == "stance":
            res = _enr.enrich_stance(df, body.target, api_key=api_key)
        else:
            raise HTTPException(400, f"Unknown enrichment kind: {body.kind}")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(res)


class NetworkRequest(BaseModel):
    mode: str = "interaction"  # "interaction" | "co_mention"
    min_edge_weight: int = 2


@app.post("/api/network/build")
def network_build(body: NetworkRequest) -> JSONResponse:
    from corpus_intel.core import network as _net
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        raise HTTPException(400, "No corpus built.")
    if body.mode == "interaction":
        graph = _net.user_interaction_graph(df)
    elif body.mode == "co_mention":
        graph = _net.co_mention_graph(df, min_edge_weight=int(body.min_edge_weight))
    else:
        raise HTTPException(400, "mode must be 'interaction' or 'co_mention'")
    return JSONResponse(graph)


@app.get("/api/multimodal/summary")
def multimodal_summary() -> JSONResponse:
    from corpus_intel.core import multimodal as _mm
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        return JSONResponse({"n_rows": 0})
    return JSONResponse(_mm.summarise_corpus(df))


@app.post("/api/language/detect")
def language_detect() -> JSONResponse:
    """Detect language per row (best-effort) and cache back to the corpus."""
    from corpus_intel.core import enrichment as _enr
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        raise HTTPException(400, "No corpus built.")
    enriched = _enr.detect_language(df)
    s.save_corpus(enriched)
    save_state()
    langs = enriched.get("_lang_iso")
    counts = {}
    if langs is not None:
        counts = langs.value_counts().head(20).to_dict()
    return JSONResponse({"rows": int(len(enriched)), "top_languages": counts})


# ─── P13: Reporting & publishing ────────────────────────────────────────────
class ReportRequest(BaseModel):
    title: str = "Corpus Intel report"
    format: str = "md"  # "md" | "html"


@app.post("/api/report/generate")
def report_generate(body: ReportRequest) -> StreamingResponse:
    from corpus_intel.core import report_generator as _rg
    s = get_state()
    api_key = s.api_key or ""
    if body.format == "html":
        content = _rg.build_html(s, title=body.title, api_key=api_key)
        media = "text/html"
        ext = "html"
    else:
        content = _rg.build_markdown(s, title=body.title, api_key=api_key)
        media = "text/markdown"
        ext = "md"
    return _download(content.encode("utf-8"), media, f"report.{ext}")


class FigureRequest(BaseModel):
    kind: str  # "bar" | "line" | "pie"
    labels: List[str]
    datasets: List[Dict[str, Any]] = []   # for bar/line
    values: List[float] = []              # for pie
    title: str = ""
    xlabel: str = ""
    ylabel: str = ""
    horizontal: bool = False
    format: str = "png"  # "png" | "svg"
    dpi: int = 300


@app.post("/api/figure/render")
def figure_render(body: FigureRequest) -> StreamingResponse:
    from corpus_intel.core import figure_export as _fe
    fmt = "png" if body.format not in ("png", "svg") else body.format
    try:
        if body.kind == "bar":
            raw = _fe.render_bar(body.labels, body.datasets, title=body.title,
                                 xlabel=body.xlabel, ylabel=body.ylabel,
                                 horizontal=body.horizontal, fmt=fmt, dpi=body.dpi)
        elif body.kind == "line":
            raw = _fe.render_line(body.labels, body.datasets, title=body.title,
                                  xlabel=body.xlabel, ylabel=body.ylabel,
                                  fmt=fmt, dpi=body.dpi)
        elif body.kind == "pie":
            raw = _fe.render_pie(body.labels, body.values, title=body.title,
                                 fmt=fmt, dpi=body.dpi)
        else:
            raise HTTPException(400, f"Unknown kind: {body.kind}")
    except ImportError as e:
        raise HTTPException(500, f"matplotlib required: {e}")
    media = "image/svg+xml" if fmt == "svg" else "image/png"
    return _download(raw, media, f"figure.{fmt}")


class QuotesRequest(BaseModel):
    label_col: str = "ai_category"
    per_label: int = 5
    method: str = "confidence"  # "confidence" | "keywords" | "random"
    keywords_per_label: Dict[str, List[str]] = {}


@app.post("/api/quotes/extract")
def quotes_extract(body: QuotesRequest) -> JSONResponse:
    from corpus_intel.core import quote_extractor as _qe
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        raise HTTPException(400, "No corpus built.")

    diag: Dict[str, Any] = {}
    label_col = body.label_col or ""
    # Auto-fall-back to the export column name when the legacy default is absent.
    if label_col and label_col not in df.columns and body.method != "keywords":
        alt = "ai_tag" if "ai_tag" in df.columns else ("topic" if "topic" in df.columns else "")
        if alt:
            diag["label_col_substituted"] = {"requested": label_col, "used": alt}
            label_col = alt

    if body.method == "confidence":
        if label_col and label_col not in df.columns:
            return JSONResponse({
                "quotes": {},
                "reason": f"missing_label_column:{label_col}",
                "hint": "Run AI coding or topic classification first, or pick a column that exists "
                        "(e.g. 'ai_tag', 'topic').",
                **diag,
            })
        out = _qe.extract_by_confidence(df, label_col=label_col, per_label=int(body.per_label))
    elif body.method == "keywords":
        if not body.keywords_per_label:
            raise HTTPException(400, "method='keywords' needs keywords_per_label.")
        out = _qe.extract_by_keywords(df, body.keywords_per_label, per_label=int(body.per_label))
    else:
        out = _qe.extract_random(df, per_label=int(body.per_label), label_col=label_col or None)

    if not out:
        return JSONResponse({
            "quotes": {},
            "reason": "no_labeled_rows",
            "hint": "The label column exists but has no non-null values. Run classification first.",
            **diag,
        })
    payload = {"quotes": out}
    payload.update(diag)
    return JSONResponse(payload)


class TableRequest(BaseModel):
    rows: List[Dict[str, Any]]
    columns: List[str] = []
    caption: str = ""
    label: str = ""
    format: str = "latex"  # "latex" | "apa_md"
    round_to: int = 3


@app.post("/api/tables/render")
def tables_render(body: TableRequest) -> JSONResponse:
    from corpus_intel.core import publication_tables as _pt
    df = pd.DataFrame(body.rows)
    if body.format == "latex":
        text = _pt.df_to_latex(df, caption=body.caption, label=body.label,
                               columns=body.columns or None, round_to=int(body.round_to))
    else:
        text = _pt.df_to_apa_markdown(df, caption=body.caption, round_to=int(body.round_to))
    return JSONResponse({"text": text, "format": body.format})


# ─── P14: Onboarding & polish ───────────────────────────────────────────────
@app.get("/api/onboarding/recipes")
def onboarding_recipes() -> JSONResponse:
    from corpus_intel.core import onboarding as _ob
    return JSONResponse({"items": _ob.list_recipes()})


@app.get("/api/onboarding/recipes/{recipe_id}")
def onboarding_recipe(recipe_id: str) -> JSONResponse:
    from corpus_intel.core import onboarding as _ob
    r = _ob.get_recipe(recipe_id)
    if not r:
        raise HTTPException(404, "Recipe not found.")
    return JSONResponse(r)


@app.get("/api/onboarding/help/{topic}")
def onboarding_help(topic: str) -> JSONResponse:
    from corpus_intel.core import onboarding as _ob
    return JSONResponse(_ob.help_for(topic))


@app.get("/api/onboarding/demo_csv")
def onboarding_demo_csv() -> StreamingResponse:
    from corpus_intel.core import onboarding as _ob
    return _download(_ob.demo_dataframe_csv(), "text/csv", "demo_corpus.csv")


@app.get("/api/undo/last")
def undo_last_list(limit: int = 20) -> JSONResponse:
    """Return recent undoable actions for the universal undo panel."""
    from corpus_intel.core import undo_log as _ul
    return JSONResponse({"items": _ul.load_tail(limit=int(limit))})


@app.get("/api/commands")
def command_palette_index() -> JSONResponse:
    """All top-level commands the Ctrl+K palette can expose."""
    items = [
        {"id": "go.home",       "label": "Go to Home",       "kind": "navigate", "page": "home"},
        {"id": "go.import",     "label": "Go to Import",     "kind": "navigate", "page": "import"},
        {"id": "go.corpus",     "label": "Go to Corpus",     "kind": "navigate", "page": "corpus"},
        {"id": "go.slicer",     "label": "Go to Slicer",     "kind": "navigate", "page": "slicer"},
        {"id": "go.coding",     "label": "Go to Coding",     "kind": "navigate", "page": "coding"},
        {"id": "go.topics",     "label": "Go to Topics",     "kind": "navigate", "page": "topics"},
        {"id": "go.analytics",  "label": "Go to Analytics",  "kind": "navigate", "page": "analytics"},
        {"id": "go.report",     "label": "Go to Report",     "kind": "navigate", "page": "report"},
        {"id": "go.settings",   "label": "Go to Settings",   "kind": "navigate", "page": "settings"},
        {"id": "action.rebuild_corpus", "label": "Rebuild corpus", "kind": "action", "page": "corpus"},
        {"id": "action.undo",   "label": "Undo last coding action", "kind": "action", "page": "coding"},
        {"id": "action.export_report", "label": "Export Markdown report", "kind": "action", "page": "report"},
        {"id": "action.demo_csv", "label": "Download demo CSV", "kind": "action", "page": "import"},
    ]
    return JSONResponse({"items": items})


# ─── P15: Didactical layer ──────────────────────────────────────────────────
@app.get("/api/didactics/decision_card/{action_id}")
def didactics_decision_card(action_id: str) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    card = _d.get_decision_card(action_id)
    if not card:
        raise HTTPException(404, f"No decision card for '{action_id}'.")
    return JSONResponse(card)


@app.get("/api/didactics/decision_cards")
def didactics_decision_cards() -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse({"items": _d.list_decision_cards()})


@app.get("/api/didactics/hints")
def didactics_hints() -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse({"items": _d.list_hints()})


@app.get("/api/didactics/hint/{metric_id}")
def didactics_hint(metric_id: str) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    h = _d.hint_for(metric_id)
    if not h:
        raise HTTPException(404, f"No hint for '{metric_id}'.")
    return JSONResponse(h)


class JourneyEntryRequest(BaseModel):
    action: str
    params: Dict[str, Any] = {}
    notes: str = ""


@app.post("/api/didactics/journey")
def didactics_journey_append(body: JourneyEntryRequest) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse(_d.journey_append(body.model_dump() if hasattr(body, "model_dump") else body.dict()))


@app.get("/api/didactics/journey")
def didactics_journey_tail(limit: int = 200) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse({"items": _d.journey_tail(limit=int(limit))})


class DecisionRegisterRequest(BaseModel):
    action_id: str
    choice: str
    reason: str = ""
    coder: str = ""


@app.post("/api/didactics/decisions")
def didactics_decision_register(body: DecisionRegisterRequest) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse(_d.register_decision(body.action_id, choice=body.choice, reason=body.reason, coder=body.coder))


@app.get("/api/didactics/decisions")
def didactics_decisions_registry(limit: int = 500) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse({"items": _d.decisions_registry(limit=int(limit))})


@app.get("/api/didactics/tour/{tour_id}")
def didactics_tour(tour_id: str) -> JSONResponse:
    from corpus_intel.core import didactics as _d
    steps = _d.get_tour(tour_id)
    if not steps:
        raise HTTPException(404, f"No tour '{tour_id}'.")
    return JSONResponse({"steps": steps})


@app.get("/api/didactics/tours")
def didactics_tours() -> JSONResponse:
    from corpus_intel.core import didactics as _d
    return JSONResponse({"items": _d.list_tours()})


@app.get("/api/didactics/duplicate_clusters")
def didactics_duplicate_clusters(threshold: float = 80.0) -> JSONResponse:
    """`threshold` accepts either 0–1 ratio (0.85) or 0–100 percent (85)."""
    from corpus_intel.core import didactics as _d
    s = get_state()
    df = s.load_corpus()
    if df is None or df.empty:
        return JSONResponse({"items": []})
    t = float(threshold)
    if 0.0 < t <= 1.0:
        t = t * 100.0
    return JSONResponse({"items": _d.cluster_duplicates(df, threshold=int(round(t)))})


@app.get("/api/session/history")
def list_session_history() -> JSONResponse:
    """List rotated session snapshots in sessions/history/."""
    from corpus_intel.constants import SESSION_HISTORY_DIR
    try:
        entries = sorted(
            (e for e in os.listdir(SESSION_HISTORY_DIR) if e.startswith("latest-") and e.endswith(".ci")),
            reverse=True,
        )
    except OSError:
        entries = []
    items = []
    for name in entries:
        p = os.path.join(SESSION_HISTORY_DIR, name)
        try:
            st = os.stat(p)
            items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    return JSONResponse({"items": items})


# ─── Codebook CRUD ──────────────────────────────────────────────────────────
def _require_codebook(s, cb_id: str):
    cb = s.codebooks.get(cb_id)
    if cb is None:
        raise HTTPException(404, f"No codebook with id '{cb_id}'.")
    return cb


def _bump_codebook_version(cb) -> str:
    """Auto-bump the codebook version when categories change.

    Answer-cache keys include codebook_version; without a bump, classifications
    cached against the old category set would be served for the new one. Semver
    patch bump when the current version parses as semver, otherwise append a
    date-time marker so old sessions still produce a distinct key.
    """
    cur = (cb.version or "").strip()
    parts = cur.split(".")
    if parts and all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        while len(nums) < 3:
            nums.append(0)
        nums[-1] += 1
        cb.version = ".".join(str(n) for n in nums)
    else:
        cb.version = f"{cur or '1.0'}+{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}"
    return cb.version


@app.get("/api/codebooks")
def list_codebooks() -> JSONResponse:
    s = get_state()
    return JSONResponse({
        "codebooks": {k: asdict(v) for k, v in s.codebooks.items()},
        "active_codebook": s.active_codebook,
    })


class CodebookCreateRequest(BaseModel):
    name: str
    version: str = "1.0"
    preset: str = ""   # "" | "hate_speech_starter"


@app.post("/api/codebooks")
def create_codebook(body: CodebookCreateRequest) -> JSONResponse:
    s = get_state()
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Codebook needs a name.")
    for cb in s.codebooks.values():
        if cb.name.strip().lower() == name.lower():
            raise HTTPException(409, f"A codebook named {name!r} already exists.")
    try:
        cb = starter_hate_speech() if body.preset == "hate_speech_starter" else new_codebook(name, version=body.version or "1.0")
        if body.preset == "hate_speech_starter":
            cb.name = name
    except CodebookError as e:
        raise HTTPException(400, str(e))
    s.codebooks[cb.codebook_id] = cb
    if not s.active_codebook:
        s.active_codebook = cb.codebook_id
    s.provenance.append(
        ProvenanceEvent(
            ts=cb.created_at,
            action="codebook_create",
            params={"codebook_id": cb.codebook_id, "name": cb.name, "preset": body.preset},
        )
    )
    save_state()
    return JSONResponse({"codebook": asdict(cb), "active_codebook": s.active_codebook})


class CodebookPatchRequest(BaseModel):
    name: Optional[str] = None
    version: Optional[str] = None


@app.get("/api/codebooks/{cb_id}")
def get_codebook(cb_id: str) -> JSONResponse:
    """Return one codebook by id, 404 if unknown."""
    s = get_state()
    cb = _require_codebook(s, cb_id)
    return JSONResponse({"codebook": asdict(cb)})


@app.patch("/api/codebooks/{cb_id}")
def patch_codebook(cb_id: str, body: CodebookPatchRequest) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "Codebook name cannot be empty.")
        for other in s.codebooks.values():
            if other.codebook_id != cb_id and other.name.strip().lower() == n.lower():
                raise HTTPException(409, f"A codebook named {n!r} already exists.")
        cb.name = n
    if body.version is not None:
        cb.version = body.version.strip() or cb.version
    save_state()
    return JSONResponse({"codebook": asdict(cb)})


@app.delete("/api/codebooks/{cb_id}")
def delete_codebook(cb_id: str) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    purged_tags = 0
    for key in list(s.tags.keys()):
        before = len(s.tags[key])
        kept = [e for e in s.tags[key] if e.get("codebook_id") != cb_id]
        purged_tags += before - len(kept)
        if kept:
            s.tags[key] = kept
        else:
            s.tags.pop(key, None)
    # AI classifications are codebook-scoped; a classification carrying the
    # deleted codebook_id no longer has a category dictionary to resolve
    # against, so it becomes opaque data. Drop it.
    purged_ai = 0
    for ri in list((s.ai_classifications or {}).keys()):
        payload = s.ai_classifications.get(ri)
        if isinstance(payload, dict) and payload.get("codebook_id") == cb_id:
            s.ai_classifications.pop(ri, None)
            purged_ai += 1
    s.codebooks.pop(cb_id, None)
    if s.active_codebook == cb_id:
        s.active_codebook = next(iter(s.codebooks), None)
    s.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="codebook_delete",
            params={
                "codebook_id": cb_id,
                "name": cb.name,
                "purged_tags": purged_tags,
                "purged_ai_classifications": purged_ai,
            },
        )
    )
    save_state()
    return JSONResponse({
        "ok": True,
        "active_codebook": s.active_codebook,
        "purged_tags": purged_tags,
        "purged_ai_classifications": purged_ai,
    })


@app.post("/api/codebooks/{cb_id}/activate")
def activate_codebook(cb_id: str) -> JSONResponse:
    s = get_state()
    _require_codebook(s, cb_id)
    s.active_codebook = cb_id
    save_state()
    return JSONResponse({"ok": True, "active_codebook": cb_id})


# ─── Categories ─────────────────────────────────────────────────────────────
class CategoryRequest(BaseModel):
    title: str
    description: str = ""
    exclusion_group: str = ""
    shortcut_key: str = ""
    color: str = ""
    cat_id: Optional[str] = None


@app.post("/api/codebooks/{cb_id}/categories")
def add_codebook_category(cb_id: str, body: CategoryRequest) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    try:
        cat = add_category(
            cb,
            body.title,
            description=body.description,
            exclusion_group=body.exclusion_group,
            shortcut_key=body.shortcut_key,
            color=body.color,
            cat_id=body.cat_id,
        )
    except CodebookError as e:
        raise HTTPException(400, str(e))
    _bump_codebook_version(cb)
    save_state()
    return JSONResponse({"category": cat, "codebook": asdict(cb)})


class CategoryPatchRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    exclusion_group: Optional[str] = None
    shortcut_key: Optional[str] = None
    color: Optional[str] = None


@app.patch("/api/codebooks/{cb_id}/categories/{cat_id}")
def patch_codebook_category(cb_id: str, cat_id: str, body: CategoryPatchRequest) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    try:
        cat = update_category(
            cb, cat_id,
            title=body.title,
            description=body.description,
            exclusion_group=body.exclusion_group,
            shortcut_key=body.shortcut_key,
            color=body.color,
        )
    except CodebookError as e:
        raise HTTPException(400, str(e))
    _bump_codebook_version(cb)
    save_state()
    return JSONResponse({"category": cat, "codebook": asdict(cb)})


@app.delete("/api/codebooks/{cb_id}/categories/{cat_id}")
def delete_codebook_category(cb_id: str, cat_id: str) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    try:
        remove_category(cb, cat_id)
    except CodebookError as e:
        raise HTTPException(404, str(e))
    for key in list(s.tags.keys()):
        kept = [e for e in s.tags[key] if not (e.get("codebook_id") == cb_id and e.get("cat_id") == cat_id)]
        if kept:
            s.tags[key] = kept
        else:
            s.tags.pop(key, None)
    _bump_codebook_version(cb)
    save_state()
    return JSONResponse({"ok": True, "codebook": asdict(cb)})


class CategoryReorderRequest(BaseModel):
    cat_ids: List[str]


@app.post("/api/codebooks/{cb_id}/categories/reorder")
def reorder_codebook_categories(cb_id: str, body: CategoryReorderRequest) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    try:
        reorder_categories(cb, body.cat_ids)
    except CodebookError as e:
        raise HTTPException(400, str(e))
    # Reorder alone doesn't change category identity — but category ordering
    # can influence prompt presentation, so bump to be safe.
    _bump_codebook_version(cb)
    save_state()
    return JSONResponse({"codebook": asdict(cb)})


class CategoryRenameRequest(BaseModel):
    new_cat_id: str


@app.post("/api/codebooks/{cb_id}/categories/{cat_id}/rename")
def rename_codebook_category(cb_id: str, cat_id: str, body: CategoryRenameRequest) -> JSONResponse:
    """Rename a category id and backfill all existing tags + AI classifications."""
    s = get_state()
    cb = _require_codebook(s, cb_id)
    new_id = (body.new_cat_id or "").strip()
    if not new_id:
        raise HTTPException(400, "new_cat_id required.")
    if cat_id == new_id:
        return JSONResponse({"ok": True, "renamed": 0, "codebook": asdict(cb)})
    existing_ids = {c.get("cat_id") for c in cb.categories}
    if cat_id not in existing_ids:
        raise HTTPException(404, f"No category '{cat_id}'.")
    if new_id in existing_ids:
        raise HTTPException(409, f"Category '{new_id}' already exists in this codebook — use merge instead.")
    for c in cb.categories:
        if c.get("cat_id") == cat_id:
            c["cat_id"] = new_id
    backfilled = 0
    for key in list(s.tags.keys()):
        for e in s.tags[key]:
            if e.get("codebook_id") == cb_id and e.get("cat_id") == cat_id:
                e["cat_id"] = new_id
                backfilled += 1
    # AI classifications schema is ai_classifications[row_idx] = {top_cat, scores: {cat_id: prob}, …}
    for ri, payload in (s.ai_classifications or {}).items():
        if not isinstance(payload, dict):
            continue
        if payload.get("codebook_id") and payload.get("codebook_id") != cb_id:
            continue
        if payload.get("top_cat") == cat_id:
            payload["top_cat"] = new_id
        scores = payload.get("scores")
        if isinstance(scores, dict) and cat_id in scores:
            scores[new_id] = scores.pop(cat_id)
    _bump_codebook_version(cb)
    save_state()
    return JSONResponse({"ok": True, "renamed": backfilled, "codebook": asdict(cb)})


class CategoryMergeRequest(BaseModel):
    from_cat_id: str
    to_cat_id: str


@app.post("/api/codebooks/{cb_id}/categories/merge")
def merge_codebook_categories(cb_id: str, body: CategoryMergeRequest) -> JSONResponse:
    """Move every tag of from_cat_id onto to_cat_id, then delete from_cat_id.
    Tags already carrying to_cat_id (same coder, same row) are deduplicated."""
    s = get_state()
    cb = _require_codebook(s, cb_id)
    if body.from_cat_id == body.to_cat_id:
        raise HTTPException(400, "from_cat_id and to_cat_id must differ.")
    ids = {c.get("cat_id") for c in cb.categories}
    if body.from_cat_id not in ids or body.to_cat_id not in ids:
        raise HTTPException(404, "Both categories must exist in this codebook.")
    moved = 0
    for key in list(s.tags.keys()):
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for e in s.tags[key]:
            if e.get("codebook_id") == cb_id and e.get("cat_id") == body.from_cat_id:
                e = dict(e)
                e["cat_id"] = body.to_cat_id
                moved += 1
            sig = (e.get("codebook_id"), e.get("cat_id"), e.get("coder"))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(e)
        if out:
            s.tags[key] = out
        else:
            s.tags.pop(key, None)
    # AI classifications: rewrite top_cat and merge probabilities.
    for payload in (s.ai_classifications or {}).values():
        if not isinstance(payload, dict):
            continue
        if payload.get("codebook_id") and payload.get("codebook_id") != cb_id:
            continue
        if payload.get("top_cat") == body.from_cat_id:
            payload["top_cat"] = body.to_cat_id
        scores = payload.get("scores")
        if isinstance(scores, dict) and body.from_cat_id in scores:
            from_p = float(scores.pop(body.from_cat_id) or 0.0)
            to_p = float(scores.get(body.to_cat_id) or 0.0)
            scores[body.to_cat_id] = min(1.0, from_p + to_p)
    # Drop from_cat_id from the codebook.
    cb.categories = [c for c in cb.categories if c.get("cat_id") != body.from_cat_id]
    _bump_codebook_version(cb)
    save_state()
    return JSONResponse({"ok": True, "moved": moved, "codebook": asdict(cb)})


# ─── Import / export ────────────────────────────────────────────────────────
@app.get("/api/codebooks/{cb_id}/export")
def export_codebook(cb_id: str) -> JSONResponse:
    s = get_state()
    cb = _require_codebook(s, cb_id)
    return JSONResponse(codebook_export_dict(cb))


class CodebookImportRequest(BaseModel):
    payload: Dict[str, Any]
    name_override: Optional[str] = None


@app.post("/api/codebooks/import")
def import_codebook_endpoint(body: CodebookImportRequest) -> JSONResponse:
    s = get_state()
    try:
        cb = codebook_import_dict(body.payload, new_id=True)
    except CodebookError as e:
        raise HTTPException(400, str(e))
    if body.name_override:
        cb.name = body.name_override.strip() or cb.name
    base = cb.name
    suffix = 1
    while any(other.name.strip().lower() == cb.name.strip().lower() for other in s.codebooks.values()):
        suffix += 1
        cb.name = f"{base} ({suffix})"
    s.codebooks[cb.codebook_id] = cb
    if not s.active_codebook:
        s.active_codebook = cb.codebook_id
    s.provenance.append(
        ProvenanceEvent(
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            action="codebook_import",
            params={"codebook_id": cb.codebook_id, "name": cb.name, "categories": len(cb.categories)},
        )
    )
    save_state()
    return JSONResponse({"codebook": asdict(cb), "warnings": validate_codebook(cb)})


# ─── Coding: tag / untag / bulk / undo / progress / row ──────────────────────
def _current_coder() -> str:
    s = get_state()
    return (s.settings.get("coder_name") or "").strip()


class TagRequest(BaseModel):
    row_idx: int
    cat_id: str
    note: Optional[str] = None


def _check_row_idx(s, row_idx: int) -> None:
    """Raise 404 if row_idx is outside the current corpus."""
    n = int(s.corpus_rows or 0)
    if n <= 0:
        raise HTTPException(400, "No corpus built — load data on the Import page first.")
    if row_idx < 0 or row_idx >= n:
        raise HTTPException(404, f"row_idx {row_idx} out of range (corpus has {n} rows).")


@app.post("/api/coding/tag")
def coding_tag(body: TagRequest) -> JSONResponse:
    s = get_state()
    _check_row_idx(s, body.row_idx)
    try:
        patch = tag_row(s, body.row_idx, body.cat_id, _current_coder(), source="manual", note=body.note)
    except CodingError as e:
        raise HTTPException(400, str(e))
    save_state()
    return JSONResponse({
        "patch": patch,
        "row_tags": row_tags(s, body.row_idx),
        "undo_available": len(s._undo_stack),
    })


@app.post("/api/coding/untag")
def coding_untag(body: TagRequest) -> JSONResponse:
    s = get_state()
    _check_row_idx(s, body.row_idx)
    try:
        patch = untag_row(s, body.row_idx, body.cat_id, _current_coder())
    except CodingError as e:
        raise HTTPException(400, str(e))
    save_state()
    return JSONResponse({
        "patch": patch,
        "row_tags": row_tags(s, body.row_idx),
        "undo_available": len(s._undo_stack),
    })


class BulkTagRequest(BaseModel):
    cat_id: str
    slice_id: Optional[str] = None
    query: Optional[str] = None
    row_indices: Optional[List[int]] = None
    dry_run: bool = False


@app.post("/api/coding/bulk")
def coding_bulk(body: BulkTagRequest) -> JSONResponse:
    s = get_state()
    df = _require_corpus()
    if "_row_idx" not in df.columns:
        raise HTTPException(400, "Corpus missing _row_idx column.")
    if body.row_indices is not None:
        n = int(s.corpus_rows or len(df))
        bad = [r for r in body.row_indices if r < 0 or r >= n]
        if bad:
            raise HTTPException(400, f"row_indices out of range: {bad[:5]}{'…' if len(bad)>5 else ''}")
        row_ids = [int(i) for i in body.row_indices]
    elif body.slice_id:
        sd = s.slices.get(body.slice_id)
        if not sd:
            raise HTTPException(404, f"No slice '{body.slice_id}'.")
        sub = _evaluate_slice(df, sd)
        row_ids = [int(i) for i in sub["_row_idx"].tolist()]
    elif body.query is not None:
        mask = _run_query_mask(df, body.query)
        sub = df[mask]
        row_ids = [int(i) for i in sub["_row_idx"].tolist()]
    else:
        raise HTTPException(400, "Pass row_indices, slice_id, or query.")
    if body.dry_run:
        return JSONResponse({"rows_targeted": len(row_ids), "dry_run": True})
    # Auto-snapshot before a bulk op so it can be reverted even after the undo
    # stack rolls over (bulk can easily touch thousands of rows).
    if len(row_ids) >= 50 and s.active_codebook:
        try:
            from corpus_intel.core import ai_checkpoints as _ck
            _ck.snapshot(
                s,
                codebook_id=s.active_codebook,
                note=f"Pre-bulk-tag · cat='{body.cat_id}' · {len(row_ids)} rows",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("pre-bulk snapshot failed: %s", e)
    try:
        result = bulk_tag(s, row_ids, body.cat_id, _current_coder(), source="bulk")
    except CodingError as e:
        raise HTTPException(400, str(e))
    save_state()
    return JSONResponse({
        "rows_targeted": len(row_ids),
        "rows_affected": result["rows_affected"],
        "cat_id": body.cat_id,
        "undo_available": len(s._undo_stack),
    })


@app.post("/api/coding/undo")
def coding_undo() -> JSONResponse:
    s = get_state()
    result = undo_last(s)
    save_state()
    return JSONResponse({
        "ok": result is not None,
        "result": result,
        "undo_available": len(s._undo_stack),
    })


@app.get("/api/coding/progress")
def coding_progress_endpoint(slice_id: str = "") -> JSONResponse:
    s = get_state()
    row_indices: Optional[List[int]] = None
    rows_in_scope_total: Optional[int] = None
    if slice_id:
        sd = s.slices.get(slice_id)
        if not sd:
            raise HTTPException(404, f"No slice '{slice_id}'.")
        df = _require_corpus()
        sub = _evaluate_slice(df, sd)
        row_indices = [int(i) for i in sub["_row_idx"].tolist()]
        rows_in_scope_total = len(row_indices)
    else:
        if s.corpus_rows:
            rows_in_scope_total = int(s.corpus_rows)
    prog = coding_progress(s, row_indices)
    prog["rows_in_scope"] = rows_in_scope_total
    prog["slice_id"] = slice_id
    prog["coders_all"] = all_coders(s)
    return JSONResponse(prog)


@app.get("/api/coding/row/{row_idx}")
def coding_row(row_idx: int) -> JSONResponse:
    s = get_state()
    _check_row_idx(s, row_idx)
    from corpus_intel.core import memos as _memos
    return JSONResponse({
        "row_idx": row_idx,
        "tags": row_tags(s, row_idx),
        "memos": _memos.row_memos(s, row_idx),
    })


# ─── Inductive memos ────────────────────────────────────────────────────────
class MemoAddRequest(BaseModel):
    row_idx: int
    text: str


class MemoEditRequest(BaseModel):
    row_idx: int
    memo_id: str
    text: str


class MemoDeleteRequest(BaseModel):
    row_idx: int
    memo_id: str


@app.post("/api/memos/add")
def memos_add(body: MemoAddRequest) -> JSONResponse:
    from corpus_intel.core import memos as _memos
    s = get_state()
    _check_row_idx(s, body.row_idx)
    try:
        entry = _memos.add_memo(
            s, body.row_idx, body.text, _current_coder(),
            codebook_id=(s.active_codebook or ""),
        )
    except _memos.MemoError as e:
        raise HTTPException(400, str(e))
    save_state()
    return JSONResponse({"memo": entry, "row_memos": _memos.row_memos(s, body.row_idx)})


@app.post("/api/memos/edit")
def memos_edit(body: MemoEditRequest) -> JSONResponse:
    from corpus_intel.core import memos as _memos
    s = get_state()
    _check_row_idx(s, body.row_idx)
    try:
        entry = _memos.edit_memo(s, body.row_idx, body.memo_id, body.text)
    except _memos.MemoError as e:
        raise HTTPException(400, str(e))
    if not entry:
        raise HTTPException(404, f"Memo '{body.memo_id}' not found on row {body.row_idx}.")
    save_state()
    return JSONResponse({"memo": entry, "row_memos": _memos.row_memos(s, body.row_idx)})


@app.post("/api/memos/delete")
def memos_delete(body: MemoDeleteRequest) -> JSONResponse:
    from corpus_intel.core import memos as _memos
    s = get_state()
    _check_row_idx(s, body.row_idx)
    ok = _memos.delete_memo(s, body.row_idx, body.memo_id)
    if not ok:
        raise HTTPException(404, f"Memo '{body.memo_id}' not found on row {body.row_idx}.")
    save_state()
    return JSONResponse({"ok": True, "row_memos": _memos.row_memos(s, body.row_idx)})


@app.get("/api/memos/row/{row_idx}")
def memos_row(row_idx: int) -> JSONResponse:
    from corpus_intel.core import memos as _memos
    s = get_state()
    _check_row_idx(s, row_idx)
    return JSONResponse({"row_idx": row_idx, "memos": _memos.row_memos(s, row_idx)})


@app.get("/api/memos")
def memos_list(
    coder: Optional[str] = None,
    codebook_id: Optional[str] = None,
    q: Optional[str] = None,
) -> JSONResponse:
    from corpus_intel.core import memos as _memos
    s = get_state()
    items = _memos.list_all(s, coder=coder, codebook_id=codebook_id, query=q)
    return JSONResponse({"items": items, "counts": _memos.counts(s)})


class IRRRequest(BaseModel):
    slice_id: Optional[str] = None
    coders: Optional[List[str]] = None


@app.post("/api/coding/irr")
def coding_irr(body: IRRRequest) -> JSONResponse:
    s = get_state()
    if not s.active_codebook or s.active_codebook not in s.codebooks:
        raise HTTPException(400, "No active codebook.")
    cb = s.codebooks[s.active_codebook]
    categories = [c["cat_id"] for c in cb.categories]

    tags_by_row: Dict[int, List[Dict[str, Any]]] = {}
    for key, entries in s.tags.items():
        try:
            ridx = int(key)
        except (TypeError, ValueError):
            continue
        tags_by_row[ridx] = entries

    if body.slice_id:
        sd = s.slices.get(body.slice_id)
        if not sd:
            raise HTTPException(404, f"No slice '{body.slice_id}'.")
        df = _require_corpus()
        sub = _evaluate_slice(df, sd)
        scope = [int(i) for i in sub["_row_idx"].tolist()]
    else:
        scope = list(tags_by_row.keys())

    overlap = coder_overlap(tags_by_row, s.active_codebook)
    coders = body.coders or sorted(overlap["coders"].keys())
    if len(coders) < 2:
        return JSONResponse({
            "ok": False,
            "message": "Need at least 2 coders who have tagged rows in the active codebook.",
            "overlap": overlap,
        })

    if len(coders) == 2:
        res = cohens_kappa_per_category(
            scope, tags_by_row, coders[0], coders[1], categories, s.active_codebook,
        )
    else:
        res = krippendorffs_alpha_per_category(
            scope, tags_by_row, coders, categories, s.active_codebook,
        )
    res["overlap"] = overlap
    res["scope_rows"] = len(scope)
    return JSONResponse(res)


class CodingSliceRequest(BaseModel):
    slice_id: str = ""


@app.post("/api/coding/slice")
def set_coding_slice(body: CodingSliceRequest) -> JSONResponse:
    s = get_state()
    if body.slice_id and body.slice_id not in s.slices:
        raise HTTPException(404, f"No slice '{body.slice_id}'.")
    s.settings["coding_slice_id"] = body.slice_id or ""
    save_state()
    return JSONResponse({"ok": True, "coding_slice_id": s.settings["coding_slice_id"]})


# ─── AI coding (Phase 5) ────────────────────────────────────────────────────
def _ai_scope_rows(s, slice_id: str, sample_size: int) -> List[tuple]:
    """Resolve (row_idx, text) tuples for an AI coding run."""
    df = _require_corpus()
    if "_row_idx" not in df.columns or "text" not in df.columns:
        raise HTTPException(400, "Corpus missing _row_idx or text columns.")
    if slice_id:
        sd = s.slices.get(slice_id)
        if not sd:
            raise HTTPException(404, f"No slice '{slice_id}'.")
        sub = _evaluate_slice(df, sd)
    else:
        sub = df
    if sample_size and sample_size > 0 and len(sub) > sample_size:
        sub = sub.sample(n=sample_size, random_state=42).sort_index()
    rows: List[tuple] = []
    for _, r in sub.iterrows():
        ri = int(r["_row_idx"])
        text = r.get("text") or ""
        rows.append((ri, str(text)))
    return rows


class AIPreflightRequest(BaseModel):
    slice_id: Optional[str] = None
    sample_size: int = 0   # 0 = all rows in scope
    batch_size: int = DEFAULT_BATCH_SIZE


@app.post("/api/coding/ai/preflight")
def coding_ai_preflight(body: AIPreflightRequest) -> JSONResponse:
    s = get_state()
    if not s.active_codebook or s.active_codebook not in s.codebooks:
        raise HTTPException(400, "No active codebook.")
    cb = s.codebooks[s.active_codebook]
    if not cb.categories:
        raise HTTPException(400, "Active codebook has no categories.")
    rows = _ai_scope_rows(s, body.slice_id or "", body.sample_size or 0)
    if not rows:
        raise HTTPException(400, "Scope is empty.")
    res = ai_preflight(rows, cb, batch_size=max(1, min(50, body.batch_size or DEFAULT_BATCH_SIZE)))
    res["scope_rows"] = len(rows)
    res["slice_id"] = body.slice_id or ""
    res["sample_size"] = body.sample_size or 0
    res["codebook_id"] = cb.codebook_id
    from corpus_intel.core import budget as _b
    res["budget"] = _b.check(s.settings, res.get("estimated_cost_usd") or 0.0)
    return JSONResponse(res)


class AIRunRequest(BaseModel):
    slice_id: Optional[str] = None
    sample_size: int = 0
    batch_size: int = DEFAULT_BATCH_SIZE
    budget_override: Optional[float] = None


@app.post("/api/coding/ai/run")
def coding_ai_run(body: AIRunRequest) -> StreamingResponse:
    s = get_state()
    if not s.api_key:
        raise HTTPException(400, "No Anthropic API key set. Add one in Settings.")
    if not s.active_codebook or s.active_codebook not in s.codebooks:
        raise HTTPException(400, "No active codebook.")
    cb = s.codebooks[s.active_codebook]
    if not cb.categories:
        raise HTTPException(400, "Active codebook has no categories.")
    rows = _ai_scope_rows(s, body.slice_id or "", body.sample_size or 0)
    if not rows:
        raise HTTPException(400, "Scope is empty.")

    from corpus_intel.core import budget as _b
    pre = ai_preflight(rows, cb, batch_size=max(1, min(50, body.batch_size or DEFAULT_BATCH_SIZE)))
    est_cost = float(pre.get("estimated_cost_usd") or 0.0)
    budget_envelope = _b.check(s.settings, est_cost)
    if budget_envelope["status"] == "block" and not _b.validate_override(s.settings, body.budget_override):
        raise HTTPException(402, f"Budget ceiling reached. {budget_envelope['message']}")

    # Reserve the estimated cost atomically so a concurrent run cannot also pass
    # preflight and collectively blow the ceiling. Released or committed on exit.
    if not _b.reserve_spend(s.settings, est_cost):
        # Only reachable if validate_override let us past the block but another
        # run snuck in first. Surface the same 402.
        env = _b.check(s.settings, est_cost)
        raise HTTPException(402, f"Budget ceiling reached. {env['message']}")

    # Snapshot tags + ai_classifications so a bad run can be reverted in one click.
    from corpus_intel.core import ai_checkpoints as _ck
    try:
        _ck.snapshot(
            s,
            codebook_id=cb.codebook_id,
            note=f"Pre-AI-run · {len(rows)} rows · slice='{body.slice_id or ''}'",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ai_checkpoints.snapshot failed: %s", e)

    def event_stream():
        try:
            for ev in run_classification(
                s, rows, cb,
                api_key=s.api_key,
                batch_size=max(1, min(50, body.batch_size or DEFAULT_BATCH_SIZE)),
            ):
                # record spend on each completed batch
                if ev.get("type") == "batch":
                    usd = float(ev.get("cost_usd") or 0)
                    if usd:
                        _b.record_spend(s.settings, usd)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") in ("batch", "done", "cache"):
                    try:
                        save_state()
                    except Exception as e:  # noqa: BLE001
                        log.warning("save_state failed mid-run: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("AI run failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            _b.release_reservation(s.settings, est_cost)
            try:
                save_state()
            except Exception:
                pass

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable proxy buffering if any
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


class SuggestCodebookRequest(BaseModel):
    goal: str = ""
    slice_id: Optional[str] = None
    sample_size: int = 40


@app.post("/api/coding/ai/suggest-codebook")
def coding_ai_suggest_codebook(body: SuggestCodebookRequest) -> JSONResponse:
    s = get_state()
    if not s.api_key:
        raise HTTPException(400, "No Anthropic API key set. Add one in Settings.")
    rows = _ai_scope_rows(s, body.slice_id or "", max(5, min(80, body.sample_size or 40)))
    sample_texts = [t for _, t in rows if t]
    if not sample_texts:
        raise HTTPException(400, "No non-empty texts in scope.")
    result = ai_suggest_codebook(body.goal or "", sample_texts, api_key=s.api_key)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "Suggestion failed.")
    return JSONResponse(result)


@app.get("/api/coding/ai/cache/stats")
def coding_ai_cache_stats() -> JSONResponse:
    return JSONResponse(ai_cache.stats())


@app.post("/api/coding/ai/cache/clear")
def coding_ai_cache_clear() -> JSONResponse:
    n = ai_cache.clear()
    return JSONResponse({"ok": True, "removed": n})


# ─── AI-run checkpoints + revert ────────────────────────────────────────────
@app.get("/api/coding/ai/checkpoints")
def coding_ai_checkpoints_list(codebook_id: str = "") -> JSONResponse:
    from corpus_intel.core import ai_checkpoints as _ck
    s = get_state()
    cb_id = codebook_id or s.active_codebook or ""
    return JSONResponse({"items": _ck.list_checkpoints(cb_id), "codebook_id": cb_id})


class CheckpointRevertRequest(BaseModel):
    checkpoint_id: str
    codebook_id: Optional[str] = None


@app.post("/api/coding/ai/checkpoints/revert")
def coding_ai_checkpoint_revert(body: CheckpointRevertRequest) -> JSONResponse:
    from corpus_intel.core import ai_checkpoints as _ck
    s = get_state()
    cb_id = body.codebook_id or s.active_codebook or ""
    summary = _ck.revert(s, codebook_id=cb_id, checkpoint_id=body.checkpoint_id)
    if summary is None:
        raise HTTPException(404, f"Checkpoint '{body.checkpoint_id}' not found.")
    save_state()
    return JSONResponse({"ok": True, **summary})


# ─── Manual coding redo ─────────────────────────────────────────────────────
@app.post("/api/coding/redo")
def coding_redo_endpoint() -> JSONResponse:
    from corpus_intel.core import coding as _cod
    s = get_state()
    result = _cod.redo_last(s)
    save_state()
    return JSONResponse({
        "ok": result is not None,
        "result": result,
        "undo_available": len(s._undo_stack),
        "redo_available": len(getattr(s, "_redo_stack", []) or []),
    })


# ─── Topics (Phase 6) ───────────────────────────────────────────────────────
def _require_topic_set(s, ts_id: str) -> TopicSet:
    ts = s.topic_sets.get(ts_id)
    if not ts:
        raise HTTPException(404, f"No topic set '{ts_id}'.")
    return ts


def _topic_scope_rows(s, slice_id: str, *, limit: int = 0, sample: int = 0) -> List[tuple]:
    """Resolve (row_idx, text) pairs for a topic scope.

    - limit: max rows to return in scope order (0 = all)
    - sample: if >0 and scope is larger, random-sample this many rows (seed=42)
    """
    df = _require_corpus()
    if "_row_idx" not in df.columns or "text" not in df.columns:
        raise HTTPException(400, "Corpus missing _row_idx or text columns.")
    if slice_id:
        sd = s.slices.get(slice_id)
        if not sd:
            raise HTTPException(404, f"No slice '{slice_id}'.")
        sub = _evaluate_slice(df, sd)
    else:
        sub = df
    if sample and sample > 0 and len(sub) > sample:
        sub = sub.sample(n=sample, random_state=42).sort_index()
    elif limit and limit > 0 and len(sub) > limit:
        sub = sub.head(limit)
    rows: List[tuple] = []
    for _, r in sub.iterrows():
        ri = int(r["_row_idx"])
        text = r.get("text") or ""
        rows.append((ri, str(text)))
    return rows


def _corpus_rows_by_idx(indices: Iterable[int]) -> Dict[int, str]:
    """Look up row texts for a set of _row_idx values. Used by the example-row endpoint."""
    df = _require_corpus()
    if "_row_idx" not in df.columns or "text" not in df.columns:
        return {}
    wanted = set(int(i) for i in indices)
    out: Dict[int, str] = {}
    if not wanted:
        return out
    sub = df[df["_row_idx"].isin(wanted)]
    for _, r in sub.iterrows():
        out[int(r["_row_idx"])] = str(r.get("text") or "")
    return out


class TopicSetCreateRequest(BaseModel):
    name: str = ""
    scope_slice_id: Optional[str] = None
    sample_size: int = DEFAULT_SAMPLE_SIZE
    target_k: int = 8
    goal: str = ""


@app.post("/api/topics/topic-sets")
def create_topic_set(body: TopicSetCreateRequest) -> JSONResponse:
    s = get_state()
    if body.scope_slice_id and body.scope_slice_id not in s.slices:
        raise HTTPException(404, f"No slice '{body.scope_slice_id}'.")
    ts = topics_core.new_topic_set(
        name=body.name,
        scope_slice_id=body.scope_slice_id or "",
        sample_size=body.sample_size,
        target_k=body.target_k,
        goal=body.goal or "",
    )
    s.topic_sets[ts.topic_set_id] = ts
    if not s.active_topic_set:
        s.active_topic_set = ts.topic_set_id
    save_state()
    return JSONResponse({"topic_set": asdict(ts), "active_topic_set": s.active_topic_set})


@app.delete("/api/topics/topic-sets/{ts_id}")
def delete_topic_set(ts_id: str) -> JSONResponse:
    s = get_state()
    _require_topic_set(s, ts_id)
    s.topic_sets.pop(ts_id, None)
    if s.active_topic_set == ts_id:
        s.active_topic_set = next(iter(s.topic_sets), None)
    save_state()
    return JSONResponse({"ok": True, "active_topic_set": s.active_topic_set})


@app.post("/api/topics/topic-sets/{ts_id}/activate")
def activate_topic_set(ts_id: str) -> JSONResponse:
    s = get_state()
    _require_topic_set(s, ts_id)
    s.active_topic_set = ts_id
    save_state()
    return JSONResponse({"ok": True, "active_topic_set": ts_id})


@app.post("/api/topics/topic-sets/{ts_id}/revert")
def revert_topic_set(ts_id: str) -> JSONResponse:
    """Restore the topic set to the snapshot taken automatically before the
    last re-induction. Only one level of history is kept; after revert the
    snapshot is cleared so a second revert is not accidentally a no-op."""
    s = get_state()
    ts = _require_topic_set(s, ts_id)
    prior = ts.prior_run or {}
    if not prior or not prior.get("topics"):
        raise HTTPException(400, "No prior run available to revert to. Re-induction takes a snapshot automatically.")
    ts.topics = [dict(t) for t in (prior.get("topics") or [])]
    ts.row_assignments = dict(prior.get("row_assignments") or {})
    ts.model = str(prior.get("model") or "")
    ts.cost_usd = float(prior.get("cost_usd") or 0.0)
    ts.status = str(prior.get("status") or "induced")
    ts.total_rows = int(prior.get("total_rows") or 0)
    ts.last_error = ""
    restored_from = prior.get("saved_at", "")
    ts.prior_run = {}
    s.provenance.append(ProvenanceEvent(
        ts=dt.datetime.now().isoformat(timespec="seconds"),
        action="topic_set_revert",
        params={"topic_set_id": ts_id, "name": ts.name, "restored_from": restored_from},
    ))
    save_state()
    return JSONResponse({
        "ok": True, "topic_set_id": ts_id,
        "topics": ts.topics, "row_assignments_count": len(ts.row_assignments),
        "restored_from": restored_from,
    })


class TopicSetPatchRequest(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    sample_size: Optional[int] = None
    target_k: Optional[int] = None
    scope_slice_id: Optional[str] = None


@app.patch("/api/topics/topic-sets/{ts_id}")
def patch_topic_set(ts_id: str, body: TopicSetPatchRequest) -> JSONResponse:
    s = get_state()
    ts = _require_topic_set(s, ts_id)
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "Topic set name cannot be empty.")
        ts.name = n[:80]
    if body.goal is not None:
        ts.goal = body.goal.strip()[:400]
    if body.sample_size is not None:
        ts.sample_size = max(20, min(500, int(body.sample_size)))
    if body.target_k is not None:
        ts.target_k = max(3, min(20, int(body.target_k)))
    if body.scope_slice_id is not None:
        if body.scope_slice_id and body.scope_slice_id not in s.slices:
            raise HTTPException(404, f"No slice '{body.scope_slice_id}'.")
        ts.scope_slice_id = body.scope_slice_id or ""
    save_state()
    return JSONResponse({"topic_set": asdict(ts)})


class TopicsPreflightRequest(BaseModel):
    # All fields optional — if topic_set_id is provided, missing fields fall
    # back to the topic set's own settings so a "preflight this topic set"
    # call can't silently estimate for the wrong scope.
    topic_set_id: Optional[str] = None
    scope_slice_id: Optional[str] = None
    sample_size: Optional[int] = None
    batch_size: Optional[int] = None


@app.post("/api/topics/preflight")
def topics_preflight(body: TopicsPreflightRequest) -> JSONResponse:
    s = get_state()
    df = _require_corpus()

    # Fall back to topic-set settings if referenced
    ts = None
    if body.topic_set_id:
        ts = s.topic_sets.get(body.topic_set_id) if isinstance(s.topic_sets, dict) else None
        if not ts:
            raise HTTPException(404, f"No topic set '{body.topic_set_id}'.")

    scope_slice_id = body.scope_slice_id if body.scope_slice_id is not None else (ts.scope_slice_id if ts else "")
    sample_size_req = body.sample_size if body.sample_size is not None else (ts.sample_size if ts else DEFAULT_SAMPLE_SIZE)
    batch_size_req  = body.batch_size  if body.batch_size  is not None else DEFAULT_CLASSIFY_BATCH

    if scope_slice_id:
        sd = s.slices.get(scope_slice_id)
        if not sd:
            raise HTTPException(404, f"No slice '{scope_slice_id}'.")
        sub = _evaluate_slice(df, sd)
        scope_rows = int(len(sub))
    else:
        scope_rows = int(len(df))
    sample_size = max(20, min(500, sample_size_req or DEFAULT_SAMPLE_SIZE))
    # Induction only ever sees `sample_size` rows; clamp so callers passing
    # a slice smaller than sample_size get an accurate estimate.
    effective_sample = min(sample_size, scope_rows) if scope_rows else sample_size
    avg_chars = 300
    if "text" in df.columns and len(df) > 0:
        sample = df["text"].dropna().astype(str).head(200)
        if len(sample) > 0:
            avg_chars = int(max(80, min(1000, sample.str.len().mean())))
    res = topics_core.preflight(
        sample_size=effective_sample,
        classify_rows=scope_rows,
        batch_size=max(5, min(60, batch_size_req or DEFAULT_CLASSIFY_BATCH)),
        avg_chars=avg_chars,
    )
    res["scope_rows"] = scope_rows
    res["scope_slice_id"] = scope_slice_id or ""
    res["topic_set_id"] = body.topic_set_id or ""
    from corpus_intel.core import budget as _b
    res["budget"] = _b.check(s.settings, res.get("estimated_cost_usd") or 0.0)
    return JSONResponse(res)


class TopicsInduceRequest(BaseModel):
    topic_set_id: str
    goal: str = ""
    budget_override: Optional[float] = None


@app.post("/api/topics/induce")
def topics_induce(body: TopicsInduceRequest) -> StreamingResponse:
    s = get_state()
    if not s.api_key:
        raise HTTPException(400, "No Anthropic API key set. Add one in Settings.")
    ts = _require_topic_set(s, body.topic_set_id)
    sample_rows = _topic_scope_rows(s, ts.scope_slice_id or "", sample=ts.sample_size)
    if not sample_rows:
        raise HTTPException(400, "Scope is empty.")
    # Auto-snapshot the prior run so a bad re-induction can be reverted. We
    # only capture one level of history — the most recent good run.
    if ts.topics:
        ts.prior_run = {
            "topics": [dict(t) for t in (ts.topics or [])],
            "row_assignments": dict(ts.row_assignments or {}),
            "model": ts.model,
            "cost_usd": float(ts.cost_usd or 0.0),
            "status": ts.status,
            "total_rows": int(ts.total_rows or 0),
            "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        }

    from corpus_intel.core import budget as _b
    # rough estimate for budget check — the real cost is tracked per batch below
    pre = topics_core.preflight(
        sample_size=ts.sample_size,
        classify_rows=ts.total_rows or len(sample_rows),
        batch_size=40,
        avg_chars=300,
    )
    est_cost = float(pre.get("estimated_cost_usd") or 0.0)
    env = _b.check(s.settings, est_cost)
    if env["status"] == "block" and not _b.validate_override(s.settings, body.budget_override):
        raise HTTPException(402, f"Budget ceiling reached. {env['message']}")
    if not _b.reserve_spend(s.settings, est_cost):
        env2 = _b.check(s.settings, est_cost)
        raise HTTPException(402, f"Budget ceiling reached. {env2['message']}")

    def event_stream():
        try:
            for ev in topics_core.induce(
                s, ts, sample_rows,
                api_key=s.api_key,
                goal=(body.goal or ts.goal or ""),
            ):
                if ev.get("type") in ("batch", "done"):
                    usd = float(ev.get("cost_usd") or 0)
                    if usd:
                        _b.record_spend(s.settings, usd)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") in ("done", "error"):
                    try:
                        save_state()
                    except Exception as e:  # noqa: BLE001
                        log.warning("save_state failed after induce: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("Topic induction failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            _b.release_reservation(s.settings, est_cost)
            try:
                save_state()
            except Exception:
                pass

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


class TopicsClassifyRequest(BaseModel):
    topic_set_id: str
    batch_size: int = DEFAULT_CLASSIFY_BATCH
    row_limit: int = 0  # 0 = classify the whole scope
    budget_override: Optional[float] = None


@app.post("/api/topics/classify")
def topics_classify(body: TopicsClassifyRequest) -> StreamingResponse:
    s = get_state()
    if not s.api_key:
        raise HTTPException(400, "No Anthropic API key set. Add one in Settings.")
    ts = _require_topic_set(s, body.topic_set_id)
    if not ts.topics:
        raise HTTPException(400, "This topic set has no topics yet. Run induction first.")
    rows = _topic_scope_rows(s, ts.scope_slice_id or "", limit=max(0, body.row_limit or 0))
    if not rows:
        raise HTTPException(400, "Scope is empty.")

    from corpus_intel.core import budget as _b
    pre = topics_core.preflight(
        sample_size=0,
        classify_rows=len(rows),
        batch_size=max(5, min(60, body.batch_size or DEFAULT_CLASSIFY_BATCH)),
        avg_chars=300,
    )
    est_cost = float(pre.get("estimated_cost_usd") or 0.0)
    env = _b.check(s.settings, est_cost)
    if env["status"] == "block" and not _b.validate_override(s.settings, body.budget_override):
        raise HTTPException(402, f"Budget ceiling reached. {env['message']}")
    if not _b.reserve_spend(s.settings, est_cost):
        env2 = _b.check(s.settings, est_cost)
        raise HTTPException(402, f"Budget ceiling reached. {env2['message']}")

    def event_stream():
        try:
            for ev in topics_core.classify(
                s, ts, rows,
                api_key=s.api_key,
                batch_size=max(5, min(60, body.batch_size or DEFAULT_CLASSIFY_BATCH)),
            ):
                if ev.get("type") == "batch":
                    usd = float(ev.get("cost_usd") or 0)
                    if usd:
                        _b.record_spend(s.settings, usd)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") in ("batch", "done", "error"):
                    try:
                        save_state()
                    except Exception as e:  # noqa: BLE001
                        log.warning("save_state failed mid-classify: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("Topic classification failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            _b.release_reservation(s.settings, est_cost)
            try:
                save_state()
            except Exception:
                pass

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


class TopicPatchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    keywords: Optional[List[str]] = None


@app.patch("/api/topics/topic-sets/{ts_id}/topics/{topic_id}")
def patch_topic(ts_id: str, topic_id: str, body: TopicPatchRequest) -> JSONResponse:
    s = get_state()
    ts = _require_topic_set(s, ts_id)
    try:
        topic = topics_core.update_topic(
            ts, topic_id,
            name=body.name,
            description=body.description,
            keywords=body.keywords,
        )
    except TopicError as e:
        raise HTTPException(400, str(e))
    save_state()
    return JSONResponse({"topic": topic})


class TopicMergeRequest(BaseModel):
    source_ids: List[str]
    target_id: str


@app.post("/api/topics/topic-sets/{ts_id}/merge")
def merge_topic(ts_id: str, body: TopicMergeRequest) -> JSONResponse:
    s = get_state()
    ts = _require_topic_set(s, ts_id)
    try:
        res = topics_core.merge_topics(ts, body.source_ids, body.target_id)
    except TopicError as e:
        raise HTTPException(400, str(e))
    save_state()
    return JSONResponse({"ok": True, "merge": res, "topic_set": asdict(ts)})


@app.get("/api/topics/topic-sets/{ts_id}/topics/{topic_id}/rows")
def topic_sample_rows(ts_id: str, topic_id: str, n: int = 5) -> JSONResponse:
    s = get_state()
    ts = _require_topic_set(s, ts_id)
    # Gather candidate indices: prefer classified assignments; fall back to example_row_indices.
    ids = [int(k) for k, v in ts.row_assignments.items() if v == topic_id]
    if not ids:
        for t in ts.topics:
            if t["topic_id"] == topic_id:
                ids = [int(v) for v in (t.get("example_row_indices") or [])]
                break
    rows_map = _corpus_rows_by_idx(ids)
    # Keep order stable by ri ascending, limited.
    out = []
    for ri in sorted(rows_map.keys())[: max(1, min(20, n or 5))]:
        out.append({"row_idx": ri, "text": rows_map[ri]})
    return JSONResponse({"topic_id": topic_id, "rows": out, "total_assigned": len(ids)})


# ─── Analytics (Phase 7) ────────────────────────────────────────────────────
# Scope resolution: every analytics endpoint accepts an optional `slice_id`.
# Missing / blank → whole corpus. Unknown → 404. This keeps the frontend
# simple (one picker → one query param).

def _attach_synthetic_cols(df: "pd.DataFrame", s) -> "pd.DataFrame":  # type: ignore[name-defined]
    """Add `tag` and `topic` columns so Analytics can slice by human outputs.

    `tag` — for the active codebook, the sorted+joined titles of every
    category placed on the row by any coder (manual or AI). Blank when no
    tag. Rows with more than one category land in a combined bucket like
    ``"Hate speech + Anti-immigration"`` — surfacing overlap rather than
    hiding it. Empty string when the corpus has no active codebook.

    `topic` — for the active topic set, the topic name assigned to the row
    (or "Other" for the out-of-cluster bucket). Empty string when no active
    topic set, or when the row hasn't been classified yet.
    """
    import pandas as pd
    if "_row_idx" not in df.columns:
        return df

    out = df

    # ---- tag column ------------------------------------------------------
    cb = s.codebooks.get(s.active_codebook) if s.active_codebook else None
    tag_by_row: Dict[int, str] = {}
    if cb and s.tags:
        title_by_id = {c.get("cat_id"): c.get("title", c.get("cat_id", "")) for c in cb.categories}
        cb_id = cb.codebook_id
        for row_key, entries in s.tags.items():
            try:
                ri = int(row_key)
            except (TypeError, ValueError):
                continue
            titles = sorted({
                title_by_id.get(e.get("cat_id"), e.get("cat_id", ""))
                for e in entries
                if e.get("cat_id") and (not cb_id or e.get("codebook_id") in (cb_id, ""))
            })
            titles = [t for t in titles if t]
            if titles:
                tag_by_row[ri] = " + ".join(titles)

    # ---- topic column ----------------------------------------------------
    ts = s.topic_sets.get(s.active_topic_set) if s.active_topic_set else None
    topic_by_row: Dict[int, str] = {}
    if ts and ts.row_assignments:
        topic_name_by_id = {t["topic_id"]: t.get("name", t["topic_id"]) for t in (ts.topics or [])}
        topic_name_by_id.setdefault("other", "Other")
        for row_key, tid in ts.row_assignments.items():
            try:
                ri = int(row_key)
            except (TypeError, ValueError):
                continue
            if tid:
                topic_by_row[ri] = topic_name_by_id.get(tid, tid)

    # Only copy + add columns that actually have content, so
    # list_analytic_columns() doesn't surface empty dimensions.
    if tag_by_row or topic_by_row:
        out = df.copy()
        if tag_by_row:
            out["tag"] = out["_row_idx"].map(tag_by_row).fillna("")
        if topic_by_row:
            out["topic"] = out["_row_idx"].map(topic_by_row).fillna("")

    return out


def _resolve_scope(slice_id: Optional[str]) -> "tuple[pd.DataFrame, Dict[str, Any]]":
    """Load the corpus DataFrame filtered to ``slice_id`` (or the whole corpus).

    Returns (df, scope_meta). The meta dict is echoed back in responses so the
    UI can label the chart and show provenance. Synthetic `tag` and `topic`
    columns are attached so downstream analytics can group by them.
    """
    df = _require_corpus()
    s = get_state()
    df = _attach_synthetic_cols(df, s)
    sid = (slice_id or "").strip()
    if not sid:
        return df, {"slice_id": "", "slice_name": "", "kind": "corpus", "rows": int(len(df))}
    sd = s.slices.get(sid)
    if not sd:
        raise HTTPException(404, f"slice {sid!r} not found")
    sub = _evaluate_slice(df, sd)
    return sub, {
        "slice_id": sd.slice_id,
        "slice_name": sd.name,
        "kind": sd.kind,
        "rows": int(len(sub)),
    }


@app.get("/api/analytics/columns")
def analytics_columns() -> JSONResponse:
    """Which corpus columns the Analytics UI should expose as dimensions."""
    s = get_state()
    df = s.load_corpus()
    if df is None:
        return JSONResponse({
            "categorical": [], "numeric": [], "datetime": [], "list": [],
            "corpus_rows": 0,
            "slices": [],
        })
    df = _attach_synthetic_cols(df, s)
    cols = list_analytic_columns(df)
    slices_out = []
    for sd in s.slices.values():
        slices_out.append({
            "slice_id": sd.slice_id,
            "name": sd.name,
            "kind": sd.kind,
            "row_count": int(sd.row_count or 0),
        })
    slices_out.sort(key=lambda r: r["name"].lower())
    cols["corpus_rows"] = int(len(df))
    cols["slices"] = slices_out
    return JSONResponse(cols)


@app.get("/api/analytics/descriptives")
def analytics_descriptives(slice_id: Optional[str] = None, top_k: int = 10) -> JSONResponse:
    df, scope = _resolve_scope(slice_id)
    try:
        result = stats_descriptives(df, top_k=max(3, min(50, int(top_k))))
    except Exception as e:
        raise HTTPException(400, f"descriptives failed: {e}")
    return JSONResponse({"scope": scope, "result": result})


@app.get("/api/analytics/compare")
def analytics_compare(slice_a_id: str, slice_b_id: str, top_k: int = 10) -> JSONResponse:
    """Side-by-side descriptives for two slices plus a delta summary."""
    df_a, scope_a = _resolve_scope(slice_a_id)
    df_b, scope_b = _resolve_scope(slice_b_id)
    k = max(3, min(50, int(top_k)))
    a = stats_descriptives(df_a, top_k=k)
    b = stats_descriptives(df_b, top_k=k)
    return JSONResponse({
        "a": {"scope": scope_a, "result": a},
        "b": {"scope": scope_b, "result": b},
        "delta": compare_descriptives(a, b),
    })


@app.get("/api/analytics/crosstab")
def analytics_crosstab(
    row: str,
    col: str,
    slice_id: Optional[str] = None,
    normalize: Optional[str] = None,
    top_k_rows: int = 25,
    top_k_cols: int = 25,
) -> JSONResponse:
    if normalize not in (None, "", "row", "col", "all"):
        raise HTTPException(400, "normalize must be one of: row, col, all")
    df, scope = _resolve_scope(slice_id)
    try:
        result = stats_crosstab(
            df, row, col,
            normalize=(normalize or None),
            top_k_rows=max(2, min(100, int(top_k_rows))),
            top_k_cols=max(2, min(100, int(top_k_cols))),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"scope": scope, "result": result})


@app.get("/api/analytics/timeseries")
def analytics_timeseries(
    slice_id: Optional[str] = None,
    freq: str = "day",
    group: Optional[str] = None,
    top_k_groups: int = 8,
    date_col: str = "created_at",
    agg: str = "count",
    value_col: Optional[str] = None,
) -> JSONResponse:
    df, scope = _resolve_scope(slice_id)
    try:
        result = stats_timeseries(
            df,
            date_col=date_col,
            freq=freq,
            group_col=(group or None),
            top_k_groups=max(1, min(20, int(top_k_groups))),
            agg=agg,
            value_col=(value_col or None),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"scope": scope, "result": result})


@app.get("/api/analytics/ngrams")
def analytics_ngrams(
    slice_id: Optional[str] = None,
    n: int = 1,
    top_k: int = 25,
    min_count: int = 2,
    drop_stopwords: bool = True,
    extra_stopwords: Optional[str] = None,
    stopword_langs: Optional[str] = None,
) -> JSONResponse:
    df, scope = _resolve_scope(slice_id)
    extras: List[str] = []
    if extra_stopwords:
        extras = [w.strip() for w in re.split(r"[,\s]+", extra_stopwords) if w.strip()]
    langs: List[str] = []
    if stopword_langs:
        langs = [w.strip().lower() for w in re.split(r"[,\s]+", stopword_langs) if w.strip()]
    try:
        result = stats_ngrams(
            df,
            n=max(1, min(3, int(n))),
            top_k=max(1, min(200, int(top_k))),
            min_count=max(1, int(min_count)),
            drop_stopwords=bool(drop_stopwords),
            extra_stopwords=extras or None,
            stopword_langs=langs or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"scope": scope, "result": result})


@app.get("/api/analytics/stopword_languages")
def analytics_stopword_languages() -> JSONResponse:
    from corpus_intel.core.stats_engine import available_stopword_languages
    return JSONResponse({"items": available_stopword_languages()})


# ─── Export & provenance (Phase 8) ──────────────────────────────────────────
def _ts_tag() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _download(content: bytes, media_type: str, filename: str) -> StreamingResponse:
    import io
    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition(filename, "export.bin")},
    )


@app.get("/api/export/preview")
def export_preview() -> JSONResponse:
    """Summarise what a bundle would contain right now (counts only, no payload)."""
    s = get_state()
    corpus_rows = int(s.corpus_rows or 0)
    codebooks = len(s.codebooks or {})
    cats = sum(len(cb.categories or []) for cb in s.codebooks.values())
    topic_sets = len(s.topic_sets or {})
    topics_total = sum(len(ts.topics or []) for ts in s.topic_sets.values())
    rows_classified = sum(len(ts.row_assignments or {}) for ts in s.topic_sets.values())
    slices = len(s.slices or {})
    events = len(s.provenance or [])
    tag_rows = sum(
        1 for entries in (s.tags or {}).values()
        if any(e.get("codebook_id") in (s.active_codebook, "") for e in entries)
    )
    return JSONResponse({
        "corpus_rows": corpus_rows,
        "codebooks": codebooks,
        "codebook_categories": cats,
        "topic_sets": topic_sets,
        "topics": topics_total,
        "rows_with_topic": rows_classified,
        "rows_with_tag": tag_rows,
        "slices": slices,
        "events": events,
        "active_codebook": s.active_codebook,
        "active_topic_set": s.active_topic_set,
        "xlsx_available": _xlsx_available(),
    })


def _xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        return False


@app.get("/api/export/corpus.csv")
def export_corpus_csv() -> StreamingResponse:
    s = get_state()
    df = s.load_corpus()
    if df is None:
        raise HTTPException(400, "No corpus built yet.")
    # Stream the CSV in 5k-row chunks so the browser can start downloading
    # immediately and we never hold the full ~35 MB in memory.
    gen = provenance_mod.tagged_corpus_csv_chunks(df, s)
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(f"corpus-tagged-{_ts_tag()}.csv", "export.csv")},
    )


@app.get("/api/export/corpus.xlsx")
def export_corpus_xlsx() -> StreamingResponse:
    s = get_state()
    df = s.load_corpus()
    if df is None:
        raise HTTPException(400, "No corpus built yet.")
    data = provenance_mod.tagged_corpus_xlsx(df, s)
    if data is None:
        raise HTTPException(
            500,
            "XLSX export requires the 'openpyxl' package — falling back to CSV is recommended.",
        )
    return _download(
        data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"corpus-tagged-{_ts_tag()}.xlsx",
    )


@app.get("/api/export/codebooks.json")
def export_codebooks_json() -> StreamingResponse:
    s = get_state()
    return _download(provenance_mod.codebooks_json(s), "application/json", f"codebooks-{_ts_tag()}.json")


@app.get("/api/export/topics.json")
def export_topics_json() -> StreamingResponse:
    s = get_state()
    return _download(provenance_mod.topics_json(s), "application/json", f"topics-{_ts_tag()}.json")


@app.get("/api/export/slices.json")
def export_slices_json() -> StreamingResponse:
    s = get_state()
    return _download(provenance_mod.slices_json(s), "application/json", f"slices-{_ts_tag()}.json")


@app.get("/api/export/provenance.md")
def export_provenance_md() -> StreamingResponse:
    s = get_state()
    data = provenance_mod.render_markdown(s).encode("utf-8")
    return _download(data, "text/markdown; charset=utf-8", f"provenance-{_ts_tag()}.md")


@app.get("/api/export/provenance.json")
def export_provenance_json() -> StreamingResponse:
    s = get_state()
    return _download(provenance_mod.provenance_json(s), "application/json", f"provenance-{_ts_tag()}.json")


class BundleRequest(BaseModel):
    corpus_csv: bool = True
    corpus_xlsx: bool = False
    codebooks: bool = True
    topics: bool = True
    slices: bool = True
    provenance: bool = True


@app.post("/api/export/bundle")
def export_bundle(body: BundleRequest) -> StreamingResponse:
    s = get_state()
    include = body.model_dump()
    needs_corpus = include.get("corpus_csv") or include.get("corpus_xlsx")
    corpus_df = s.load_corpus() if needs_corpus else None
    if needs_corpus and corpus_df is None:
        raise HTTPException(400, "No corpus built yet — uncheck the corpus options or build a corpus first.")
    data = provenance_mod.build_bundle(s, corpus_df, include=include)
    s.provenance.append(ProvenanceEvent(
        ts=dt.datetime.now().isoformat(timespec="seconds"),
        action="export_bundle",
        params={
            "items": [k for k, v in include.items() if v],
            "bytes": len(data),
            "corpus_rows": int(s.corpus_rows or 0),
        },
    ))
    save_state()
    return _download(data, "application/zip", f"corpus-intel-bundle-{_ts_tag()}.zip")
