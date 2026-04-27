"""Shared constants — paths, version, default model routing."""
from __future__ import annotations

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PACKAGE_DIR, "static")
SESSION_DIR = os.path.join(PACKAGE_DIR, "sessions")
DATASETS_DIR = os.path.join(SESSION_DIR, "datasets")
CACHE_DIR = os.path.join(SESSION_DIR, "cache")

SESSION_FILE = os.path.join(SESSION_DIR, "latest.ci")
SESSION_HISTORY_DIR = os.path.join(SESSION_DIR, "history")
UNDO_LOG_FILE = os.path.join(SESSION_DIR, "undo.jsonl")
CORPORA_DIR = os.path.join(SESSION_DIR, "corpora")   # per-snapshot parquet
SNAPSHOTS_FILE = os.path.join(SESSION_DIR, "snapshots.json")

SESSION_HISTORY_KEEP = 10            # rotate: keep last N on disk
UNDO_LOG_MAX = 500                   # rolling cap
DUCKDB_THRESHOLD_ROWS = 50_000       # pandas below, DuckDB-backed above

os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(DATASETS_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(SESSION_HISTORY_DIR, exist_ok=True)
os.makedirs(CORPORA_DIR, exist_ok=True)

# Model routing — see CLAUDE.md
AI_MODELS = {
    "mapping":        "claude-haiku-4-5-20251001",
    "quick_label":    "claude-haiku-4-5-20251001",
    "classification": "claude-haiku-4-5-20251001",
    "codebook":       "claude-sonnet-4-6",
    "topic_induce":   "claude-sonnet-4-6",
    "topic_classify": "claude-haiku-4-5-20251001",
    "topic_summary":  "claude-sonnet-4-6",
    "narrative":      "claude-sonnet-4-6",
}

APP_NAME = "Corpus Intel"
APP_VERSION = "0.1.0"

# Upload limits
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB per file
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".jsonl", ".ndjson"}
