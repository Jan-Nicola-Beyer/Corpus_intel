"""Report generator (P13) — assemble a Markdown report from corpus state.

Uses Claude Sonnet for the narrative sections (per CLAUDE.md: Sonnet is the
"publication-grade prose" model). Everything else (numbers, tables, method
description) is deterministic.

Structure:
    # <Title>
    ## Abstract        — 150-word summary, AI-drafted
    ## Method          — deterministic description (sources, sizes, dates)
    ## Findings        — per-topic / per-slice highlights, AI-summarised
    ## Data quality    — dedup stats, coder agreement, language mix
    ## Appendix        — provenance log, codebook

Export formats: Markdown (always) + optional HTML via simple renderer.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from corpus_intel.ai.claude_client import call_claude
from corpus_intel.constants import AI_MODELS

log = logging.getLogger("corpus_intel.reports")

_REPORT_SYSTEM = (
    "You are a research writer drafting a publication-grade report section. "
    "Tone: precise, neutral, citation-ready. Avoid speculation. Use concrete "
    "numbers from the supplied context. Return plain Markdown."
)


def _fmt_int(x: Any) -> str:
    try:
        return f"{int(x):,}"
    except Exception:
        return str(x)


def method_section(state: Any) -> str:
    c = getattr(state, "corpus_stats", {}) or {}
    datasets = getattr(state, "datasets", {}) or {}
    sources = list(getattr(state, "corpus_sources", []) or [])
    date_range = _date_range(state)
    lines = [
        "## Method",
        "",
        f"- **Datasets combined:** {len(sources)} source dataset(s).",
        f"- **Total rows in:** {_fmt_int(c.get('input_rows', 0))}",
        f"- **Exact-ID duplicates removed:** {_fmt_int(c.get('exact_post_id_duplicates', 0))}",
        f"- **Near-duplicates removed:** {_fmt_int(c.get('near_text_duplicates', 0))}",
        f"- **Date range:** {date_range[0]} to {date_range[1]}",
        "",
        "### Sources",
        "",
    ]
    for sid in sources:
        meta = datasets.get(sid)
        if not meta:
            continue
        lines.append(f"- `{sid}` — {getattr(meta, 'original_filename', '')} ({_fmt_int(getattr(meta, 'row_count', 0))} rows, source: {getattr(meta, 'source_id', 'unknown')})")
    return "\n".join(lines)


def findings_section(state: Any, *, api_key: str = "", top_k_topics: int = 5) -> str:
    df = state.load_corpus() if hasattr(state, "load_corpus") else None
    topics = getattr(state, "topics", None) or {}
    parts: List[str] = ["## Findings", ""]
    if not topics:
        parts.append("_No topic model has been run yet — run Topics → Induce to include findings._")
        return "\n".join(parts)

    # Pick top topics by size
    items = list((topics.get("topics") or topics).items() if isinstance(topics, dict) else topics)
    items.sort(key=lambda kv: -(kv[1].get("size") if isinstance(kv[1], dict) else 0))
    items = items[: max(1, int(top_k_topics))]
    for tid, t in items:
        if not isinstance(t, dict):
            continue
        label = t.get("label") or tid
        size = t.get("size") or 0
        keywords = ", ".join((t.get("keywords") or [])[:8])
        summary = t.get("summary") or ""
        parts.append(f"### {label}")
        parts.append(f"*{_fmt_int(size)} posts — keywords: {keywords}*")
        parts.append("")
        parts.append(summary or "_(no summary yet — run the topic summariser)_")
        parts.append("")
    return "\n".join(parts)


def quality_section(state: Any) -> str:
    c = getattr(state, "corpus_stats", {}) or {}
    rows = getattr(state, "corpus_rows", None)
    if rows is None:
        try: rows = int(c.get("final_rows") or 0)
        except Exception: rows = 0
    lines = ["## Data quality", "",
             f"- Final corpus size: **{_fmt_int(rows)}** rows.",
             f"- Exact-ID duplicate rate: {_pct(c.get('exact_post_id_duplicates', 0), c.get('input_rows', 0))}",
             f"- Near-duplicate rate: {_pct(c.get('near_text_duplicates', 0), c.get('input_rows', 0))}"]
    return "\n".join(lines)


def provenance_section(state: Any, limit: int = 30) -> str:
    events = list(getattr(state, "provenance", []) or [])
    events = events[-limit:]
    if not events:
        return "## Appendix — Provenance\n\n_No actions recorded yet._"
    lines = ["## Appendix — Provenance", "", "| When | Action | Params |", "|---|---|---|"]
    for ev in events:
        ts = getattr(ev, "ts", "") or ""
        action = getattr(ev, "action", "") or ""
        params = getattr(ev, "params", {}) or {}
        # render params compact
        try:
            p = json.dumps(params, ensure_ascii=False, default=str)[:120]
        except Exception:
            p = str(params)[:120]
        lines.append(f"| {ts} | {action} | `{p}` |")
    return "\n".join(lines)


def abstract_section(state: Any, *, api_key: str = "", word_limit: int = 150) -> str:
    """Ask Sonnet for a short abstract. Falls back to a template if no API key."""
    rows = 0
    if hasattr(state, "corpus_rows"): rows = int(state.corpus_rows or 0)
    stats = getattr(state, "corpus_stats", {}) or {}
    topics = getattr(state, "topics", {}) or {}
    n_topics = len(topics.get("topics", {}) if isinstance(topics.get("topics"), dict) else topics)

    if not api_key:
        return (
            "## Abstract\n\n"
            f"This report analyses {_fmt_int(rows)} posts combined from {len(getattr(state, 'corpus_sources', []) or [])} dataset(s), "
            f"after removing {_fmt_int(stats.get('exact_post_id_duplicates', 0))} exact and "
            f"{_fmt_int(stats.get('near_text_duplicates', 0))} near-duplicate rows. "
            f"A topic model surfaced {n_topics or 'several'} themes; per-topic summaries appear in Findings."
        )
    try:
        prompt = (
            f"Write a {word_limit}-word abstract for a research report based on these facts:\n"
            f"- {_fmt_int(rows)} posts in the final corpus\n"
            f"- {_fmt_int(stats.get('input_rows', 0))} input rows; "
            f"{_fmt_int(stats.get('exact_post_id_duplicates', 0))} exact + "
            f"{_fmt_int(stats.get('near_text_duplicates', 0))} near-duplicates removed\n"
            f"- {n_topics} topics identified\n"
            f"Output Markdown starting with '## Abstract'. Neutral academic tone."
        )
        text = call_claude(_REPORT_SYSTEM, prompt, task="report", api_key=api_key, max_tokens=500)
        return text.strip()
    except Exception as e:
        log.warning("abstract generation failed: %s", e)
        return abstract_section(state, api_key="", word_limit=word_limit)


def _date_range(state: Any):
    df = state.load_corpus() if hasattr(state, "load_corpus") else None
    if df is None or df.empty or "posted_at" not in df.columns:
        return ("–", "–")
    d = pd.to_datetime(df["posted_at"], errors="coerce").dropna()
    if d.empty:
        return ("–", "–")
    return (d.min().strftime("%Y-%m-%d"), d.max().strftime("%Y-%m-%d"))


def _pct(n, d):
    try:
        n, d = int(n), int(d)
        return f"{(n / d * 100):.2f}% ({_fmt_int(n)} / {_fmt_int(d)})" if d else f"—"
    except Exception:
        return "—"


def build_markdown(state: Any, *, title: Optional[str] = None, api_key: str = "") -> str:
    title = title or "Corpus Intel report"
    today = dt.date.today().isoformat()
    parts = [
        f"# {title}", "",
        f"_Generated {today} by Corpus Intel_", "",
        abstract_section(state, api_key=api_key), "",
        method_section(state), "",
        findings_section(state, api_key=api_key), "",
        quality_section(state), "",
        provenance_section(state), "",
    ]
    return "\n\n".join(p for p in parts if p)


def build_html(state: Any, **kwargs) -> str:
    md = build_markdown(state, **kwargs)
    try:
        import markdown  # type: ignore
        body = markdown.markdown(md, extensions=["tables", "fenced_code"])
    except ImportError:
        # crude fallback: wrap in <pre>
        import html as _h
        body = f"<pre>{_h.escape(md)}</pre>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Corpus Intel report</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:800px;margin:2rem auto;padding:0 1rem;color:#0f0f0f;line-height:1.55}"
        "h1,h2,h3{color:#0f0f0f}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:0.25rem 0.5rem}code{background:#f4f4f4;padding:0 0.2rem;border-radius:3px}"
        "</style></head><body>"
        f"{body}</body></html>"
    )
