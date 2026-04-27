# Corpus Intel — Complete Build Plan

> **Claude Code entry point:** open the folder `C:/Users/beyer/AntiGravity/Corpus Intel` as working directory. `CLAUDE.md` auto-loads with project rules. Read this file (`CODING_PLAN.md`) at the start of every session, then continue from the current phase. Phase status is tracked in `BUILD_STATUS.md` (created during Phase 0).

---

## 1. Vision

A **lightweight, web-hosted research workstation** that lets a non-expert researcher go from "a folder of CSV exports" to a "tagged, analyzed, publication-ready corpus" without writing code or running local ML models. Everything runs in pandas + Anthropic API. Total container size target: <400MB.

### Target researcher personas
- **Journalist** cross-referencing platform exports for a story
- **NGO analyst** coding harassment/hate-speech content against a codebook
- **Grad student** exploring thematic structure of protest corpora
- **Policy researcher** quantifying narrative shifts over time

### Non-goals
- No real-time scraping (users bring their own exports)
- No authentication / multi-user (v1 is single-researcher)
- No paper-ready statistics beyond descriptives + IRR (leave inference to R/SPSS)

---

## 2. Architecture

```
Corpus Intel/
├── CLAUDE.md                    # project context (auto-read)
├── CODING_PLAN.md               # this file
├── BUILD_STATUS.md              # phase checklist (created Phase 0)
├── requirements.txt
├── launch.bat                   # Windows launcher
├── corpus_intel/
│   ├── __init__.py
│   ├── web_server.py            # FastAPI app, port 8788
│   ├── app_state.py             # central state, sessions/latest.ci
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── claude_client.py     # all Anthropic calls
│   │   └── prompts.py           # all system prompts as constants
│   ├── core/
│   │   ├── __init__.py
│   │   ├── ingest.py            # file upload, encoding/delimiter detection
│   │   ├── sources/             # one module per data source (see §4)
│   │   │   ├── brandwatch.py
│   │   │   ├── twitter.py
│   │   │   ├── meta.py
│   │   │   ├── tiktok.py
│   │   │   ├── youtube.py
│   │   │   ├── reddit.py
│   │   │   ├── mastodon.py
│   │   │   ├── news.py
│   │   │   └── generic.py
│   │   ├── schema.py            # canonical column schema + mapper
│   │   ├── merge.py             # concat + dedupe
│   │   ├── slicer.py            # boolean query engine
│   │   ├── filters.py           # regex/platform/lang/date
│   │   ├── codebook.py          # codebook import/export/apply
│   │   ├── coding.py            # manual tag operations
│   │   ├── ai_coding.py         # Claude-assisted tagging
│   │   ├── topics.py            # sample-induce-classify pipeline
│   │   ├── stats_engine.py      # descriptives, cross-tabs, IRR
│   │   ├── answer_cache.py      # ported from BC4D
│   │   └── provenance.py        # reproducibility log
│   ├── static/
│   │   ├── index.html
│   │   ├── app.js
│   │   ├── styles.css
│   │   └── logos/
│   │       └── isd-logo.png
│   └── sessions/
│       └── latest.ci            # JSON session state
└── tests/
    └── fixtures/                # small sample exports per source
```

### State model (`app_state.py`)
```python
@dataclass
class AppState:
    project_name: str
    datasets: dict[str, DataFrameMeta]  # id -> metadata; DataFrames cached in-memory
    corpus_df: pd.DataFrame | None       # merged working corpus
    schema_mapping: dict[str, dict]      # dataset_id -> {source_col: canonical_col}
    slices: dict[str, SliceDef]          # named saved slices
    codebooks: dict[str, Codebook]       # named codebooks
    active_codebook: str | None
    tags: dict[int, list[str]]           # row_idx -> tag list
    ai_classifications: dict[int, dict]  # row_idx -> {tag, confidence, model_version}
    topics: dict[str, TopicResult]       # topic-run id -> result
    provenance: list[ProvenanceEvent]    # reproducibility log
    api_key: str
    settings: dict
```

### API surface (FastAPI endpoints)
- `POST /api/datasets/upload` → multipart, returns dataset_id + detected source
- `GET /api/datasets` → list of datasets with metadata
- `POST /api/datasets/{id}/mapping` → set or get column mapping (AI-assisted available)
- `POST /api/corpus/merge` → build corpus_df from selected datasets
- `POST /api/slices` → create saved slice (body: boolean query string)
- `GET /api/slices` → list saved slices with row counts
- `POST /api/codebooks` / `GET /api/codebooks/{id}` / `PUT` / `DELETE`
- `POST /api/coding/tag` → manual tag a row
- `POST /api/coding/bulk` → tag by regex/query
- `POST /api/coding/ai/preflight` → cost estimate
- `POST /api/coding/ai/run` → stream classification results via SSE
- `POST /api/topics/induce` → sample-based topic discovery (SSE)
- `POST /api/topics/classify` → assign remaining rows (SSE)
- `GET /api/analytics/descriptives` / `GET /api/analytics/crosstab`
- `GET /api/export/{format}` → bundle exports (csv, xlsx, codebook.json, provenance.md)
- `GET /api/state` → session snapshot for frontend

---

## 3. Frontend layout

**Sidebar navigation** (match BC4D/ISD Intel exactly):
1. **Home** — project overview, resume session, recent activity
2. **Import** — upload datasets, detect source, map columns
3. **Corpus** — view merged data, row-level inspection
4. **Slicer** — boolean queries, saved slices, diff
5. **Codebook** — manage codebooks, manual tagging UI
6. **AI Coding** — preflight + run Claude classification
7. **Topics** — sample-induce-classify, topic viewer
8. **Analytics** — cross-tabs, time-series, slice comparisons
9. **Export** — bundles, provenance log, publication outputs
10. **Settings** — API key, theme, model routing

Each section is a lazy-rendered `<section>` toggled by sidebar click, exactly like BC4D's `app.js` router.

---

## 4. Data source integrations

Each source lives in `core/sources/<name>.py` and exposes:
```python
def detect(df: pd.DataFrame, filename: str) -> float  # 0-1 confidence
def normalize(df: pd.DataFrame) -> pd.DataFrame       # to canonical schema
SOURCE_ID = "brandwatch"
CANONICAL_COLUMNS = {...}  # source col -> canonical col
```

### Canonical schema (v1)
```
post_id, platform, source_type, author_id, author_handle, author_name,
text, language, created_at, url,
like_count, share_count, comment_count, view_count,
in_reply_to, parent_id,
media_urls, hashtags, mentions,
country, region, sentiment_source  # native sentiment from source if present
```

### Source adapters to ship in v1
| Source | File signature | Notes |
|---|---|---|
| **Brandwatch** | CSV with `Query Name`, `Date`, `Full Text`, `Sentiment` | Most common input |
| **Twitter/X** | Twitter academic archive `.js` + `.csv`, or 3rd-party tools (tweepy-dump, snscrape CSV) | Multi-file detect |
| **Meta / CrowdTangle** | CSV with `Page Name`, `Message`, `Post Created Date` | Legacy but still used |
| **TikTok Research API** | JSONL + CSV export | Growing |
| **YouTube** | YouTube Data API dump (CSV from yt-dlp or API wrappers) | Video + comment modes |
| **Reddit** | Pushshift-style CSV/JSON, or PRAW dumps | Submission/comment schemas |
| **Mastodon** | Instance CSV export, mstdn-dl JSON | Fediverse |
| **News articles** | GDELT, MediaCloud, RSS aggregator CSVs | `title + body + url` |
| **Generic** | Any CSV/TSV/XLSX | Fallback, manual mapping |

**Detection flow:** on upload, all detectors run in parallel → highest-confidence source wins → UI shows "Detected: Brandwatch (95% confidence). Change?" Claude Haiku is asked only if all detectors score < 0.6.

---

## 5. AI strategy (cost-controlled, Anthropic-only)

### Caching layers
1. **Prompt caching** (Anthropic native) for: system prompts, codebooks, taxonomies. ~90% discount on cached tokens.
2. **Answer cache** (`core/answer_cache.py`, ported from BC4D): key = `sha256(row_text + prompt_version)`. Re-runs are free.
3. **Sample-first default:** every AI action defaults to 200-row sample. User explicitly clicks "Run full corpus" to commit.

### Cost estimation (preflight — mandatory)
Every AI action shows:
```
Rows to process: 12,483
Model: claude-haiku-4-5-20251001
Batches: 625 × 20 rows
Cached input tokens: ~18,000 (codebook)
Fresh input tokens: ~2,800,000
Output tokens: ~150,000
Estimated cost: $0.74 (±20%)
Estimated time: 4–6 min
```

### Prompt patterns
- **Column mapping** — Haiku, ~300 input tokens, one call per dataset
- **Codebook suggestion** — Sonnet, 100-row sample input, structured JSON output
- **Classification** — Haiku, 20 rows per batch, codebook in cached prefix
- **Topic induction** — Sonnet, 200-row sample, returns 4–8 cluster definitions
- **Topic summarization** — Sonnet, per-cluster with example quotes
- **Slice narrative** — Sonnet, on-demand

---

## 6. Phased build plan

> Mark each phase complete in `BUILD_STATUS.md` before moving on. Ask the user to confirm phase completion — do not auto-advance.

---

### Phase 0 — Scaffold (~1 day)
**Goal:** working sidebar + empty sections, server starts, logo shows.

Tasks:
1. Create `corpus_intel/` package structure per §2.
2. Write `requirements.txt`: `fastapi`, `uvicorn[standard]`, `python-multipart`, `pandas`, `openpyxl`, `chardet`, `anthropic`, `pydantic`.
3. Copy ISD logo from `C:/Users/beyer/AntiGravity/ISD Intel/isd_intel/static/logos/isd-logo.png` to `static/logos/`.
4. Build minimal `web_server.py` with `/api/state` and static file mount.
5. Port BC4D's `index.html` + `styles.css`; replace title with "Corpus Intel – Research Corpus Platform", update sidebar with 10 items from §3.
6. Port BC4D's `app.js` navigation skeleton; stub each section with placeholder text.
7. Write `launch.bat`: `python -m uvicorn corpus_intel.web_server:app --host 127.0.0.1 --port 8788`.
8. Create `BUILD_STATUS.md` with checkbox per phase.

**Acceptance:** `launch.bat` opens a working sidebar at http://127.0.0.1:8788, all 10 sections navigable, ISD logo visible, no console errors.

---

### Phase 1 — Ingest & schema mapping (~3 days)
**Goal:** upload any CSV/XLSX, auto-detect source, map columns to canonical schema.

Tasks:
1. `core/ingest.py` — handle upload, detect encoding (`chardet`), delimiter, header row.
2. `core/schema.py` — canonical schema constants, fuzzy-match source cols to canonical.
3. `core/sources/*.py` — one adapter per source in §4 list. Each has `detect()` and `normalize()`.
4. `ai/prompts.py` — `COLUMN_MAPPING_PROMPT` for fallback.
5. `web_server.py` endpoints: `/api/datasets/upload`, `/api/datasets`, `/api/datasets/{id}/mapping`.
6. Frontend Import section: drag-drop upload, detected-source badge, column-mapping table with "Suggest with AI" button.
7. Show quality flags: missing `text`, duplicate `post_id`, unparseable dates.

**Acceptance:** user uploads `test_brandwatch.csv`, `test_twitter.csv`, `test_meta.csv`; each detected correctly; column mapping table populates; user can override; mapping saves to session.

---

### Phase 2 — Corpus merge & filters (~2 days)
**Goal:** combine selected datasets into one working corpus with filters.

Tasks:
1. `core/merge.py` — concat with NaN-fill, dedupe by `post_id` and near-dupe by text hash (simhash or ngram Jaccard).
2. `core/filters.py` — regex text search, platform/language/date-range.
3. Endpoints: `/api/corpus/merge`, `/api/corpus/filter`.
4. Frontend Corpus section: dataset selector, merge button, row count banner, filter panel, paginated table view with row detail modal.

**Acceptance:** merge 3 datasets → dedupe count reported → filters live-update row count and table.

---

### Phase 3 — Slicer (~2 days)
**Goal:** Brandwatch-syntax boolean queries, named saved slices.

Tasks:
1. `core/slicer.py` — port parser from `datalens_v3_opt`. Support quoted phrases, `AND`/`OR`/`NOT`, parentheses, case-insensitive.
2. Endpoints: `POST /api/slices`, `GET /api/slices`, `DELETE /api/slices/{id}`.
3. Frontend Slicer section: query input with syntax hint, result count preview, save-as-slice form, saved-slices chip list, slice diff view.

**Acceptance:** query like `("hate speech" OR hass*) AND NOT test` returns correct row count; saved slice appears in list and persists across reload.

---

### Phase 4 — Codebook & manual coding (~3 days)
**Goal:** first-class codebook objects, manual tagging UI with keyboard shortcuts and IRR.

Tasks:
1. `core/codebook.py` — `Codebook` dataclass: name, version, categories (id, title, description, exclusion group, shortcut key). Import/export JSON.
2. `core/coding.py` — tag/untag row, bulk tag by query, 50-level undo stack, audit trail.
3. `core/stats_engine.py` — inter-coder reliability (Cohen's κ for 2 coders, Krippendorff's α for >2).
4. Endpoints: codebook CRUD, `/api/coding/tag`, `/api/coding/bulk`, `/api/coding/irr`.
5. Frontend Codebook section: list codebooks, import/export, edit categories. Coding UI: focus row + text, category buttons with shortcut hints, undo button, progress bar, IRR panel when 2+ coders present.

**Acceptance:** import starter codebook, tag 50 rows manually using keyboard, bulk-tag 20 by regex, export codebook + tags to JSON, re-import on fresh session.

---

### Phase 5 — AI coding (~3 days)
**Goal:** Claude-assisted classification with caching, preflight, streaming, overrides.

Tasks:
1. `ai/claude_client.py` — retry + backoff, prompt caching helpers.
2. `ai/prompts.py` — `CLASSIFICATION_SYSTEM_PROMPT`, `CODEBOOK_SUGGESTION_PROMPT`.
3. `core/answer_cache.py` — port from BC4D.
4. `core/ai_coding.py` — batch classifier (20 rows/call), sample mode default, cost estimator.
5. Endpoints: `/api/coding/ai/preflight`, `/api/coding/ai/run` (SSE), `/api/coding/ai/suggest-codebook`.
6. Frontend AI Coding section: preflight card showing cost + time, "Run on sample (200 rows)" and "Run full corpus" buttons, live progress, override table showing AI tag + confidence with quick-fix UI.

**Acceptance:** AI classifies 200-row sample in <60s, preflight cost matches actual within 20%, answer cache hit on re-run shows $0.00, override edits persist.

---

### Phase 6 — Topic modelling (~3 days)
**Goal:** sample-induce-classify topic discovery, viewer with narratives.

Tasks:
1. `core/topics.py` — two-pass (induce from 200-sample → classify rest in batches). Port BC4D's `analyze_matched_likert` match patterns where useful.
2. `ai/prompts.py` — `TOPIC_INDUCTION_PROMPT`, `TOPIC_SUMMARY_PROMPT`.
3. Endpoints: `/api/topics/induce` (SSE), `/api/topics/classify` (SSE), `/api/topics/{id}`.
4. Frontend Topics section: run-config card (sample size, max topics), induction progress, topic list with counts + AI-written description + 3 example quotes each, export topics to JSON.

**Acceptance:** topic run on 1k-row corpus produces 4–8 coherent topics in <3 min, each with quotes, user can rename/merge topics.

---

### Phase 7 — Analytics (~2 days)
**Goal:** pure-pandas descriptives and cross-tabs; no AI unless user asks for narrative.

Tasks:
1. `core/stats_engine.py` (extend) — descriptives, cross-tab (tag × platform, tag × month), n-gram top terms per slice, time-series.
2. Endpoints: `/api/analytics/descriptives`, `/api/analytics/crosstab`, `/api/analytics/timeseries`, `/api/analytics/ngrams`.
3. Frontend Analytics section: Chart.js charts (reuse BC4D's `wrapLabel` + dynamic height), slice selector, comparison mode (slice A vs slice B).

**Acceptance:** charts render with full-text labels, cross-tab exports to XLSX.

---

### Phase 8 — Export & provenance (~2 days)
**Goal:** reproducible research output.

Tasks:
1. `core/provenance.py` — log every transformation (upload, merge, filter, tag, AI-classify, topic-induce) with timestamps, params, row counts.
2. Endpoints: `/api/export/csv`, `/api/export/xlsx`, `/api/export/codebook`, `/api/export/provenance`, `/api/export/bundle` (ZIP).
3. Frontend Export section: bundle builder (checkboxes for what to include), download button, provenance preview pane.

**Acceptance:** bundle ZIP contains tagged corpus CSV, codebook JSON, topics JSON, provenance markdown with reproducible method description.

---

### Phase 9 — Polish (~2 days)
**Goal:** onboarding + guardrails.

Tasks:
1. Home page: "Start project" wizard → pick goal (build corpus / code existing / explore themes) → open scoped flow.
2. Glossary tooltips on every stats term, AI action, clustering parameter.
3. Empty-state messages for every section ("No datasets yet. Go to Import to get started.")
4. Filter-returns-zero-rows helper ("Try removing the date filter.")
5. Resume-session banner on startup.

**Acceptance:** fresh user with no prior exposure can go upload → merge → code sample → export in under 30 minutes without asking for help.

---

## 7. Total estimate

**~21 working days** for the full app. Critical path to usable MVP = Phases 0–5 (~14 days).

---

## 8. How to start a fresh Claude session

1. Open folder `C:/Users/beyer/AntiGravity/Corpus Intel` in Claude Code.
2. `CLAUDE.md` auto-loads with project rules.
3. Tell Claude: **"Read CODING_PLAN.md and BUILD_STATUS.md, then continue from the current phase."** (First session: "Read CODING_PLAN.md and start Phase 0.")
4. Claude will read the plan, check status, and execute the next phase. Confirm completion per phase before allowing the next one to start.

---

## 9. Open decisions deferred to later

These are **intentionally not in v1** to keep scope tight. Document the rationale if/when added:
- Multi-user auth + sharing
- Live API ingestion (vs. CSV import)
- SQLite backend for large corpora
- Statistical inference beyond descriptives
- LLM-free offline mode
- Mobile/tablet layout

---

## 10. Things to copy from BC4D Intel verbatim
- `core/answer_cache.py` (rename to match, adjust cache dir)
- `static/styles.css` (change only the app title + minor tweaks)
- `static/index.html` skeleton (swap sidebar items)
- `app.js` navigation router, `wrapLabel` helper, Chart.js setup, dynamic-height chart containers
- The ISD logo PNG
- `claude_client.py` (retry + backoff pattern)
- Preflight cost estimate UI pattern from BC4D's AI Engine section
