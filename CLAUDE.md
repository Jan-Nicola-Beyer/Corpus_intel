# Corpus Intel — Project Context (auto-read by Claude Code)

## What this app is
Corpus Intel is a **standalone web app** for non-expert researchers (social scientists, NGO analysts, journalists) to build, clean, code, and analyze social-media corpora. Input: exports from any source (Brandwatch, Twitter/X, Meta, TikTok, YouTube, Reddit, Mastodon, news APIs, generic CSV/XLSX). Output: tagged datasets, topic models, analytics, reproducibility logs, publication-ready exports.

## Critical architecture rules
1. **All data flows through a single `AppState` object** — persisted to `sessions/latest.ci` (JSON). Never use globals.
2. **All Claude API calls go through `ai/claude_client.py`** — never call `anthropic` directly from endpoints or modules.
3. **All system prompts live in `ai/prompts.py` as constants** — never hardcode strings in logic files.
4. **All heavy work is pure-Python pandas/scipy** — LLMs only do semantic tasks that can't be done deterministically.
5. **No local LLMs. No torch, transformers, or BERTopic.** Anthropic API only.
6. **No databases for v1.** JSON sessions on disk. Add SQLite later only if sessions exceed ~50MB.

## Framework & Style
- **Backend:** FastAPI + uvicorn, port 8788, Python 3.10+
- **Frontend:** Vanilla JS + Chart.js (same pattern as `BC4D Intel/bc4d_intel/static/app.js`)
- **Design system:** match ISD Intel + BC4D Intel glassmorphism light theme
  - Primary accent: `#C8175D` (ISD pink)
  - Background: `#f7f7f5`
  - Text: `#0f0f0f`
  - Card bg: `rgba(255,255,255,0.6)` with backdrop-filter blur
  - Logo: reuse `C:/Users/beyer/AntiGravity/ISD Intel/isd_intel/static/logos/isd-logo.png`
- **Tables & charts:** reuse BC4D patterns — `white-space:normal; word-break:break-word; line-height:1.35`, dynamic-height Y-axis labels with `wrapLabel` helper and `autoSkip:false`.

## Model routing — do not deviate without asking
| Task | Model |
|------|-------|
| Column mapping, codebook suggestion, quick labels | `claude-haiku-4-5-20251001` |
| Topic induction on sample, slice/topic summaries | `claude-sonnet-4-6` |
| Report narrative, publication-grade prose | `claude-sonnet-4-6` |
| Boolean query parsing, stats, filters, merges, dedupe | No API — pure Python |

## Cost control — mandatory
Every AI action MUST:
1. Show a **preflight estimate** before running (rows × tokens × price).
2. Use **prompt caching** for codebooks and system prompts.
3. Check `core/answer_cache.py` before re-classifying the same (row_hash, prompt_version).
4. Default to **sample mode** (200 rows) unless user explicitly clicks "Run on full corpus."

## Build order
Always follow the phases in `CODING_PLAN.md`. Complete and confirm each phase before starting the next.
Do not write logic in Phase 0 — scaffold only.

## Full spec
See `CODING_PLAN.md` for complete file structure, per-phase tasks, data source integrations, and acceptance criteria.

## Reference apps
Before writing any UI or state code, read these for pattern reference:
- `C:/Users/beyer/AntiGravity/BC4D Intel/bc4d_intel/web_server.py` — FastAPI server pattern
- `C:/Users/beyer/AntiGravity/BC4D Intel/bc4d_intel/static/app.js` — frontend nav + state + chart patterns
- `C:/Users/beyer/AntiGravity/BC4D Intel/bc4d_intel/static/styles.css` — design system
- `C:/Users/beyer/AntiGravity/BC4D Intel/bc4d_intel/core/answer_cache.py` — caching to port
- `C:/Users/beyer/AntiGravity/ISD Intel/isd_intel/` — sibling app, same design language

## Source tool to transfer from
`C:/Users/beyer/AntiGravity/V2/datalens_v3_opt/` — existing desktop app (~10.4k LOC). Port the useful bits:
- Boolean query parser (Brandwatch syntax)
- Canonical column schema
- Manual tagging keyboard shortcuts
- Inter-coder reliability calculations
Discard: all PyTorch / transformers / BERTopic / Tkinter UI.
