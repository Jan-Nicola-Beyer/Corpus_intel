"""Provenance + export bundle.

Builds on the ``ProvenanceEvent`` log that other modules already append to
``AppState.provenance``. This module:

* renders the log as human-readable markdown (methods-section prose),
* assembles the tagged-corpus CSV/XLSX (with `tag` + `topic` columns),
* packages codebooks, topic sets and slices as JSON,
* zips all of that into a reproducibility bundle.

Everything is pure-python — the ZIP is built in memory and returned as bytes
so the HTTP endpoint can stream it without touching disk.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import zipfile
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from corpus_intel.app_state import AppState, ProvenanceEvent

# ─── Readable labels per action ────────────────────────────────────────────
_ACTION_LABEL: Dict[str, str] = {
    "upload":                "Dataset uploaded",
    "dataset_delete":        "Dataset deleted",
    "mapping_save":          "Schema mapping saved",
    "mapping_ai":             "AI-suggested mapping",
    "corpus_merge":          "Corpus merged",
    "corpus_clear":          "Corpus cleared",
    "slice_save":            "Slice saved",
    "slice_delete":          "Slice deleted",
    "slice_sample":          "Sample slice drawn",
    "slice_split":           "Corpus split into equal chunks",
    "slice_compose":         "Slices composed",
    "codebook_create":       "Codebook created",
    "codebook_update":       "Codebook edited",
    "codebook_delete":       "Codebook deleted",
    "codebook_import":       "Codebook imported",
    "bulk_tag":              "Bulk tag applied",
    "manual_tag":            "Manual tag",
    "ai_classify":           "AI classification run",
    "ai_suggest_codebook":   "AI codebook suggestion",
    "topic_set_create":      "Topic run created",
    "topic_induce":          "Topics induced",
    "topic_classify":        "Rows classified into topics",
    "topic_rename":          "Topic renamed",
    "topic_merge":           "Topics merged",
    "topic_set_delete":      "Topic run deleted",
    "export_bundle":         "Reproducibility bundle exported",
}


def _label(action: str) -> str:
    return _ACTION_LABEL.get(action, action.replace("_", " ").capitalize())


def _summarise_params(action: str, params: Dict[str, Any]) -> str:
    """Compact one-line human summary for a param dict."""
    if not params:
        return ""
    p = params
    def _g(*keys, default=None):
        for k in keys:
            if k in p:
                v = p[k]
                if v is None or v == "":
                    continue
                return v
        return default

    if action == "upload":
        return f"{_g('filename','dataset_id', default='')} · {_g('rows', default=0)} rows · detected {_g('source_id', default='generic')} ({float(_g('confidence', default=0) or 0):.2f})"
    if action == "corpus_merge":
        return f"{_g('datasets', default=[])} → {_g('rows_out','rows', default=0)} rows (dedup removed {_g('removed', default=0)})"
    if action == "slice_save":
        return f"{_g('name', default='(unnamed)')} · {_g('kind', default='query')} · {_g('row_count', default=0)} rows"
    if action == "slice_sample":
        return f"{_g('name', default='(unnamed)')} · {_g('method', default='random')} · {_g('row_count', default=0)} rows"
    if action == "slice_split":
        return f"K={_g('k', default=0)} · overlap={_g('overlap', default=0)} · {_g('rows_per_chunk', default=0)} rows/chunk"
    if action == "slice_compose":
        return f"{_g('op', default='and')} · {_g('a', default='?')} ∘ {_g('b', default='?')} → {_g('row_count', default=0)} rows"
    if action == "bulk_tag":
        return f"category={_g('category', default='?')} · {_g('rows_tagged', default=0)} rows · query=`{_g('query', default='')}`"
    if action == "ai_classify":
        rows = _g("rows_classified", "rows", default=0)
        return f"{rows} rows · model {_g('model', default='?')} · ${float(_g('cost_usd', default=0) or 0):.4f}"
    if action == "topic_induce":
        return f"{_g('topic_count', default=0)} topics · sample {_g('sample_size', default=0)} · model {_g('model', default='?')}"
    if action == "topic_classify":
        return f"{_g('rows_classified', default=0)} rows · {_g('batches', default=0)} batches · ${float(_g('cost_usd', default=0) or 0):.4f}"
    if action == "topic_merge":
        return f"merged {_g('sources', default=[])} → {_g('target', default='?')}"
    if action == "export_bundle":
        items = _g("items", default=[]) or []
        kb = int(_g("bytes", default=0) or 0) // 1024
        return f"{len(items)} items · {kb} KB · includes: {', '.join(items)}"

    # Fallback — dump up to ~4 pairs
    items = [(k, v) for k, v in p.items() if not isinstance(v, (list, dict)) or isinstance(v, list) and len(v) <= 4]
    short = [f"{k}={v}" for k, v in items[:4]]
    return " · ".join(short)


# ─── Markdown renderer ─────────────────────────────────────────────────────
def render_markdown(state: AppState) -> str:
    """Render the provenance log as a methods-style markdown document."""
    events = list(state.provenance or [])
    now = dt.datetime.now().isoformat(timespec="seconds")
    lines: List[str] = []
    lines.append(f"# Corpus Intel — reproducibility log")
    lines.append("")
    lines.append(f"_Exported {now}_")
    lines.append("")

    # ── Corpus snapshot ───────────────────────────────────────────────────
    lines.append("## Corpus")
    lines.append("")
    if state.corpus_rows:
        lines.append(f"- Rows: **{state.corpus_rows:,}**")
        if state.corpus_built_at:
            lines.append(f"- Built: {state.corpus_built_at}")
        if state.corpus_sources:
            lines.append(f"- Sources ({len(state.corpus_sources)}): {', '.join(state.corpus_sources)}")
        cs = state.corpus_stats or {}
        if cs.get("removed_exact"):
            lines.append(f"- Exact duplicates removed: {cs['removed_exact']:,}")
        if cs.get("removed_near"):
            lines.append(f"- Near-duplicates removed: {cs['removed_near']:,}")
    else:
        lines.append("_No corpus has been built yet._")
    lines.append("")

    # ── Codebooks ─────────────────────────────────────────────────────────
    lines.append("## Codebooks")
    lines.append("")
    if state.codebooks:
        for cb in state.codebooks.values():
            cats = getattr(cb, "categories", []) or []
            active = " (active)" if state.active_codebook == cb.codebook_id else ""
            lines.append(f"- **{cb.name}**{active} — {len(cats)} categories")
            for c in cats[:50]:
                lines.append(f"    - `{c.get('cat_id','')}` — {c.get('title','')}")
    else:
        lines.append("_No codebooks defined._")
    lines.append("")

    # ── Topic sets ────────────────────────────────────────────────────────
    lines.append("## Topic runs")
    lines.append("")
    if state.topic_sets:
        for ts in state.topic_sets.values():
            active = " (active)" if state.active_topic_set == ts.topic_set_id else ""
            topics = getattr(ts, "topics", []) or []
            assigned = len(getattr(ts, "row_assignments", {}) or {})
            lines.append(f"- **{ts.name}**{active} — {len(topics)} topics, {assigned:,} rows classified · sample {ts.sample_size} · model {ts.model or '(not yet run)'}")
            for t in topics[:20]:
                lines.append(f"    - `{t.get('topic_id','')}` — {t.get('name','')} ({t.get('count',0):,})")
    else:
        lines.append("_No topic runs._")
    lines.append("")

    # ── Slices ────────────────────────────────────────────────────────────
    lines.append("## Saved slices")
    lines.append("")
    if state.slices:
        for sl in state.slices.values():
            lines.append(f"- **{sl.name}** · {sl.kind} · {sl.row_count:,} rows")
            if sl.kind == "query" and sl.query:
                lines.append(f"    - Query: `{sl.query}`")
            elif sl.kind == "sample" and sl.spec:
                lines.append(f"    - Sample spec: `{json.dumps(sl.spec, ensure_ascii=False)[:160]}`")
    else:
        lines.append("_No slices._")
    lines.append("")

    # ── Event log ─────────────────────────────────────────────────────────
    lines.append("## Event log")
    lines.append("")
    if not events:
        lines.append("_No events recorded yet._")
    else:
        lines.append(f"_{len(events)} events, oldest first._")
        lines.append("")
        lines.append("| When | What | Details |")
        lines.append("|---|---|---|")
        for e in events:
            ts = getattr(e, "ts", "")
            action = getattr(e, "action", "")
            params = getattr(e, "params", {}) or {}
            summary = _summarise_params(action, params).replace("|", "\\|")
            lines.append(f"| {ts} | {_label(action)} | {summary} |")
    lines.append("")
    return "\n".join(lines)


# ─── Tagged corpus export helpers ──────────────────────────────────────────
def _attach_export_cols(df: pd.DataFrame, state: AppState) -> pd.DataFrame:
    """Add readable `tag`, `topic`, `ai_tag`, `ai_confidence` columns.

    Mirrors the logic in web_server._attach_synthetic_cols but also exposes
    the AI-only column so exporters can distinguish manual vs AI tags.
    """
    if "_row_idx" not in df.columns:
        return df
    out = df.copy()

    # tag (manual + AI combined, joined titles)
    cb = state.codebooks.get(state.active_codebook) if state.active_codebook else None
    if cb and state.tags:
        title_by_id = {c.get("cat_id"): c.get("title", c.get("cat_id", "")) for c in cb.categories}
        cb_id = cb.codebook_id
        tag_by_row: Dict[int, str] = {}
        for row_key, entries in state.tags.items():
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
        out["tag"] = out["_row_idx"].map(tag_by_row).fillna("")

    # AI-only tag + confidence
    if state.ai_classifications:
        ai_tag_by_row: Dict[int, str] = {}
        ai_conf_by_row: Dict[int, float] = {}
        title_by_id = {}
        if cb:
            title_by_id = {c.get("cat_id"): c.get("title", c.get("cat_id", "")) for c in cb.categories}
        for row_key, rec in state.ai_classifications.items():
            try:
                ri = int(row_key)
            except (TypeError, ValueError):
                continue
            cat_id = rec.get("cat_id") or ""
            ai_tag_by_row[ri] = title_by_id.get(cat_id, cat_id) or ""
            if rec.get("confidence") is not None:
                try:
                    ai_conf_by_row[ri] = float(rec["confidence"])
                except (TypeError, ValueError):
                    pass
        out["ai_tag"] = out["_row_idx"].map(ai_tag_by_row).fillna("")
        if ai_conf_by_row:
            out["ai_confidence"] = out["_row_idx"].map(ai_conf_by_row)

    # topic (active topic set)
    ts = state.topic_sets.get(state.active_topic_set) if state.active_topic_set else None
    if ts and ts.row_assignments:
        topic_name_by_id = {t["topic_id"]: t.get("name", t["topic_id"]) for t in (ts.topics or [])}
        topic_name_by_id.setdefault("other", "Other")
        topic_by_row: Dict[int, str] = {}
        for row_key, tid in ts.row_assignments.items():
            try:
                ri = int(row_key)
            except (TypeError, ValueError):
                continue
            if tid:
                topic_by_row[ri] = topic_name_by_id.get(tid, tid)
        out["topic"] = out["_row_idx"].map(topic_by_row).fillna("")

    return out


def _prepare_csv_frame(df: pd.DataFrame, state: AppState) -> pd.DataFrame:
    out = _attach_export_cols(df, state)
    # Flatten list-valued columns so they CSV cleanly.
    for col in ("hashtags", "mentions", "media_urls"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: "|".join(str(x) for x in v) if isinstance(v, (list, tuple)) else (v or ""))
    return out


def tagged_corpus_csv(df: pd.DataFrame, state: AppState) -> bytes:
    out = _prepare_csv_frame(df, state)
    buf = io.BytesIO()
    # UTF-8 BOM so Excel opens the file cleanly.
    buf.write("\ufeff".encode("utf-8"))
    out.to_csv(buf, index=False, encoding="utf-8")
    return buf.getvalue()


def tagged_corpus_csv_chunks(df: pd.DataFrame, state: AppState, chunk_rows: int = 5000):
    """Yield CSV bytes in chunks so large exports can stream to the client
    instead of materialising the whole ~35 MB payload in memory first.

    First yield contains the UTF-8 BOM + header + first block of rows; all
    subsequent yields are row-only blocks with no header.
    """
    out = _prepare_csv_frame(df, state)
    total = len(out)
    if total == 0:
        yield "\ufeff".encode("utf-8")
        yield out.to_csv(index=False, encoding="utf-8").encode("utf-8")
        return
    # Header + first chunk
    first_end = min(chunk_rows, total)
    head = out.iloc[:first_end]
    header_buf = io.BytesIO()
    header_buf.write("\ufeff".encode("utf-8"))
    head.to_csv(header_buf, index=False, encoding="utf-8")
    yield header_buf.getvalue()
    # Remaining chunks, no header
    for start in range(first_end, total, chunk_rows):
        stop = min(start + chunk_rows, total)
        chunk_buf = io.BytesIO()
        out.iloc[start:stop].to_csv(chunk_buf, index=False, header=False, encoding="utf-8")
        yield chunk_buf.getvalue()


def tagged_corpus_xlsx(df: pd.DataFrame, state: AppState) -> Optional[bytes]:
    """XLSX export. Returns None if the `openpyxl` engine isn't available —
    the caller should fall back to CSV."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return None
    out = _attach_export_cols(df, state)
    for col in ("hashtags", "mentions", "media_urls"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: "|".join(str(x) for x in v) if isinstance(v, (list, tuple)) else (v or ""))
    # Excel cannot store tz-aware datetimes — strip the timezone.
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_datetime64tz_dtype(s):
            try:
                out[col] = s.dt.tz_convert("UTC").dt.tz_localize(None)
            except Exception:
                out[col] = s.dt.tz_localize(None)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        out.to_excel(w, sheet_name="corpus", index=False)
    return buf.getvalue()


# ─── JSON dumps ────────────────────────────────────────────────────────────
def _json_safe(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def codebooks_json(state: AppState) -> bytes:
    payload = {
        "active_codebook": state.active_codebook,
        "codebooks": [_json_safe(cb) for cb in state.codebooks.values()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def topics_json(state: AppState) -> bytes:
    payload = {
        "active_topic_set": state.active_topic_set,
        "topic_sets": [_json_safe(ts) for ts in state.topic_sets.values()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def slices_json(state: AppState) -> bytes:
    payload = [_json_safe(sl) for sl in state.slices.values()]
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def provenance_json(state: AppState) -> bytes:
    payload = [_json_safe(e) for e in (state.provenance or [])]
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# ─── Bundle assembly ───────────────────────────────────────────────────────
_DEFAULT_INCLUDE = {
    "corpus_csv":   True,
    "corpus_xlsx":  False,
    "codebooks":    True,
    "topics":       True,
    "slices":       True,
    "provenance":   True,
}


def _readme(state: AppState, include: Dict[str, bool]) -> bytes:
    lines: List[str] = []
    lines.append("# Corpus Intel — export bundle")
    lines.append("")
    lines.append(f"Exported: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Contents")
    lines.append("")
    if include.get("corpus_csv"):
        lines.append("- `corpus_tagged.csv` — the merged corpus with `tag`, `ai_tag`, `ai_confidence` and `topic` columns.")
    if include.get("corpus_xlsx"):
        lines.append("- `corpus_tagged.xlsx` — same as the CSV in Excel format.")
    if include.get("codebooks"):
        lines.append("- `codebooks.json` — all codebooks and categories.")
    if include.get("topics"):
        lines.append("- `topics.json` — every topic run, its topics and row assignments.")
    if include.get("slices"):
        lines.append("- `slices.json` — saved query / sample / combo slices.")
    if include.get("provenance"):
        lines.append("- `provenance.md` — human-readable log of every step you took.")
        lines.append("- `provenance.json` — raw event log (machine-readable).")
    lines.append("")
    lines.append("## Reproducing the analysis")
    lines.append("")
    lines.append("1. Re-upload the source exports listed in `provenance.md` under *Dataset uploaded*.")
    lines.append("2. Apply the same schema mappings (stored per-dataset in your Corpus Intel session).")
    lines.append("3. Merge the datasets with the same dedupe settings.")
    lines.append("4. Replay the slices, codebook, AI runs and topic runs in the order shown in the event log.")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def build_bundle(
    state: AppState,
    corpus_df: Optional[pd.DataFrame] = None,
    *,
    include: Optional[Dict[str, bool]] = None,
) -> bytes:
    """Build a ZIP bundle as bytes. Items can be toggled via ``include``."""
    opts = {**_DEFAULT_INCLUDE, **(include or {})}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", _readme(state, opts))

        if corpus_df is not None and opts.get("corpus_csv"):
            zf.writestr("corpus_tagged.csv", tagged_corpus_csv(corpus_df, state))
        if corpus_df is not None and opts.get("corpus_xlsx"):
            xlsx = tagged_corpus_xlsx(corpus_df, state)
            if xlsx is not None:
                zf.writestr("corpus_tagged.xlsx", xlsx)
        if opts.get("codebooks"):
            zf.writestr("codebooks.json", codebooks_json(state))
        if opts.get("topics"):
            zf.writestr("topics.json", topics_json(state))
        if opts.get("slices"):
            zf.writestr("slices.json", slices_json(state))
        if opts.get("provenance"):
            zf.writestr("provenance.md", render_markdown(state).encode("utf-8"))
            zf.writestr("provenance.json", provenance_json(state))
    return buf.getvalue()
