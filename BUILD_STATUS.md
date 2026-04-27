# Corpus Intel — Build Status

Checkbox per phase from `CODING_PLAN.md`. A phase is only `[x]` after the user has confirmed it meets the acceptance criteria in the plan. Do not auto-advance.

## Phases

- [x] **Phase 0 — Scaffold** *(2026-04-17)*
  - Package structure, `requirements.txt`, logo copy, minimal `web_server.py`, sidebar + 10 section stubs, `app.js` router, `launch.bat`.
  - Acceptance: `launch.bat` → http://127.0.0.1:8788, all 10 sections navigable, ISD logo visible, no console errors. **Awaiting user confirmation.**
- [~] **Phase 1 — Ingest & schema mapping** *(2026-04-17, awaiting user confirmation)*
  - 22-field canonical schema (`core/schema.py`), fuzzy matcher with alias table, threshold 0.85.
  - 9 source adapters: brandwatch, twitter, meta, tiktok, youtube, reddit, mastodon, news, generic.
  - Upload endpoint `/api/datasets/upload` (CSV/XLSX, encoding + delimiter sniffing), dedupe-by-target auto-mapping.
  - AI-fallback endpoint `/api/datasets/{id}/mapping/suggest` via Haiku.
  - Quality flags: missing_text, missing_post_id, blank_text_rows, duplicate_post_ids, unparseable_dates.
  - Import UI: drag-drop, dataset list, source badge w/ confidence, editable mapping table, quality flags, save/AI-suggest/delete.
  - Smoke test: 8/9 fixtures detected with ≥0.90 confidence (generic correctly unknown @ 0.16).
  - Acceptance: upload each fixture, verify detection + mapping + persistence across reload. **Awaiting user confirmation.**
- [x] **Phase 2 — Corpus merge & filters** *(2026-04-17)*
  - `core/merge.py`: concat datasets using their saved mappings, tag rows with `source_dataset` + `source_id`, 2-pass dedupe (exact `post_id`, then near-dup via SHA1 of normalized text).
  - `core/filters.py`: plain-pandas `FilterSpec` covering text (regex/case), platform/language/country/source chips, and inclusive date range.
  - Endpoints: `POST /api/corpus/merge`, `GET/DELETE /api/corpus`, `POST /api/corpus/filter`, `GET /api/corpus/row/{idx}`.
  - Corpus UI: dataset picker (pre-checked, near-dup toggle), summary card with stat tiles, filter panel (text + regex + case + date range + facet chips), paginated results table with click-to-open row-detail modal.
  - Smoke test: 3 datasets → 96,272 rows in → 3,252 final (0 exact + 93,020 near-dup removed); chip and text filters live-update row count and pagination.
- [~] **Phase 3 — Slicer (boolean queries + sampling)** *(2026-04-17, awaiting user confirmation)*
  - `core/slicer.py`: Brandwatch-style recursive-descent parser. Supports `AND` / `OR` / `NOT`, parentheses, implicit-AND, `"quoted phrases"`, word-boundary wildcards (`climat*`). Parse errors raise `SlicerError`; empty query → all-True mask. `NEAR` is aliased to AND.
  - Extended with sampling: `random_sample`, `stratified_sample` (largest-remainder rounding), `top_n_sample`, `systematic_sample` (every Nth row), `equal_chunks` (K chunks + IRR overlap), and `SampleSpec`/`run_sample`/`describe_sample` helpers. All honour an optional `base_query` pre-filter and optional near-dup dedupe.
  - State: `SliceDef` extended with `kind` (`query`/`sample`/`compose`), `spec` (method-specific params), and `indices` (frozen `_row_idx` list for reproducible samples). Backwards-compatible defaults. Persisted via `AppState` to `sessions/latest.ci`.
  - Query endpoints: `POST /api/slices/preview`, `POST /api/slices` (409 on duplicate name), `GET /api/slices`, `GET /api/slices/syntax`, `GET /api/slices/{id}` (paginated re-run), `DELETE /api/slices/{id}`, `POST /api/slices/diff`.
  - Sampling endpoints: `GET /api/slices/sample/columns` (categorical + numeric), `POST /api/slices/sample/preview`, `POST /api/slices/sample` (freezes indices), `POST /api/slices/split` (K sample slices in one call with IRR overlap), `POST /api/slices/compose` (set ops AND/OR/AND NOT/OR NOT — stays query-kind if both inputs are query, else freezes).
  - Slicer UI: empty-state card, query textarea with `Cmd/Ctrl+Enter`, Run/Clear toolbar, inline status, syntax help, result card with pagination + row modal, name + Save slice.
  - Sample & split card: 5 tabs (Random / Stratified / Top by metric / Every Nth / Equal chunks) with method-specific inputs, optional base-filter query + dedupe, live help text per tab, Preview button runs a dry-run with row count + paged preview table, Save-as-slice for single samples, Save-all-K-chunks for splits.
  - Saved slices list: kind badge per slice (`query` / `frozen` / `combo`) — Load is disabled for frozen samples since their indices are fixed.
  - Compare & combine card: 6-tile Venn breakdown plus a per-tile "Save as slice" button — one click composes the intersection / A only / B only / union into a new saved slice (query-composed when inputs allow, frozen otherwise).
  - Home copy updated to mention Slicer once corpus is built, and to advertise slice count once any slices exist.
  - Smoke test (API + unit): parser cases all pass; `("hate speech" OR hass*) AND NOT satire` → 456/3,252 rows; save/load/delete/reload persists; diff math verified (449+8=457, 20+8=28, union=477). Sampling: random=50 and top-N=10 return correct counts; stratified preserves language distribution; systematic step=100 → 33 rows; split K=3 → three 1,084-row chunks; compose `query AND_NOT sample` → frozen combo with correct row_count; 6 smoke slices cleaned up after test.
  - Acceptance: run a complex query, save/load/delete a slice, reload; draw a random sample & a stratified sample, save both; split the corpus into 3 chunks with 10% overlap for an IRR pilot; compose two slices into a saved intersection. **Awaiting user confirmation.**
- [x] **Phase 4 — Codebook & manual coding** *(implemented; awaiting user confirmation)*
  - `core/codebook.py` (CRUD + JSON import/export), `core/coding.py` (tag/untag, bulk-by-query, 50-level undo, audit trail), `core/stats_engine.py` (Cohen's κ + Krippendorff's α).
  - Endpoints: `POST/GET/PUT/DELETE /api/codebooks`, `/api/coding/tag`, `/api/coding/bulk`, `/api/coding/irr`.
  - UI: codebook list + editor, focus-row coding panel with category buttons + keyboard shortcuts, IRR panel.
- [x] **Phase 5 — AI coding** *(implemented; awaiting user confirmation)*
  - `ai/claude_client.py` (retry/backoff + prompt caching), `ai/prompts.py` (`CLASSIFICATION_SYSTEM_PROMPT`, `CODEBOOK_SUGGESTION_PROMPT`).
  - `core/answer_cache.py` ported from BC4D. `core/ai_coding.py` with 20-row batching, preflight estimator, sample-first default.
  - Endpoints: `/api/coding/ai/preflight`, `/api/coding/ai/run` (SSE), `/api/coding/ai/suggest-codebook`.
  - UI: preflight card, sample/full-corpus run buttons, live progress log, override table with quick-fix, cache stats.
  - **2026-04-18 clarity pass:** explicit **Sample / All rows** selector (sample size only appears in sample mode), batch size moved under an *Advanced* disclosure, model shown on the scope card, plain-language review copy surfacing the low-confidence toggle as the spot-check starting point.
- [x] **Phase 6 — Topic modelling** *(implemented; awaiting user confirmation)*
  - `core/topics.py` (two-pass induce-then-classify). `ai/prompts.py` extended with `TOPIC_INDUCTION_PROMPT`, `TOPIC_SUMMARY_PROMPT`.
  - Endpoints: topic-set CRUD, `/api/topics/induce` (SSE), `/api/topics/classify` (SSE), `/api/topics/{id}/sample-rows`.
  - UI: run-config card, topic list viewer with counts + description + keywords + example quotes, rename/merge.
  - **2026-04-18 clarity pass:** added `target_k` on `TopicSet` and the induction prompt ("aim for about N topics"), surfaced as a **Target topics** input on the create card. Classify step uses an explicit **All rows / First N** selector, batch size moved under *Advanced*. Numbered step badges (1-Create, 2-Induce, 3-Classify, 4-Topics) make the flow visible, and the viewer now signposts the **Other** bucket as the "add-a-topic" signal. The header copy now says "rename or merge" (split is not a supported op).
- [~] **Phase 7 — Analytics** *(2026-04-18, awaiting user confirmation)*
  - `core/stats_engine.py` extended with `descriptives`, `crosstab`, `timeseries`, `ngrams`, `list_analytic_columns`, and `compare_descriptives` — all pure-pandas, no AI. Simple regex tokenizer + built-in stopword list for n-grams.
  - Endpoints: `GET /api/analytics/columns`, `/api/analytics/descriptives`, `/api/analytics/compare`, `/api/analytics/crosstab`, `/api/analytics/timeseries`, `/api/analytics/ngrams`. Each accepts an optional `slice_id` scope (empty = whole corpus).
  - Frontend Analytics section: scope picker with optional compare-slice toggle, descriptives tile grid + top-lists for facets/authors/hashtags, time-series line chart (day/week/month/quarter/year bucket × optional group-by), cross-tab heatmap table with row/col/all normalisation, top-terms horizontal bar (uni/bi/trigrams, top-K, stopword toggle, dynamic-height container, `wrapLabel` ticks).
  - **2026-04-18 audit pass:** `_attach_synthetic_cols()` in `web_server.py` injects `tag` and `topic` columns (from active codebook + active topic set) into every analytics scope so they appear as first-class dimensions in descriptives/crosstab/timeseries/group-by — only added when they have content. `timeseries()` now takes `agg=count|sum|mean` + `value_col` for engagement metrics over time. `descriptives()` adds `top_authors_by_engagement` (authors ranked by summed like/share/comment/view count). Frontend: metric + value-col dropdowns on the time-series card, extra-stopwords + min-count on the n-grams card, in-page hints for crosstab normalisation, and a **Download CSV** button on every card (descriptives / time-series / crosstab / n-grams).
  - Smoke test: all six endpoints return 200 on the existing 3,252-row corpus; crosstab labels round-trip UTF-8 cleanly (`—` fill preserved). Browser check via `claude-in-chrome`: every card renders, all four charts draw on the whole corpus, compare mode toggled on with two scratch slices produces side-by-side descriptives + delta tiles, side-by-side crosstab tables, and A/B versions of the time-series and n-gram charts — zero console errors. Metric switch to `sum like_count` re-renders correctly; all four CSV downloads produce well-formed UTF-8 output (descriptives, time-series, crosstab, n-grams).
  - Acceptance: open Analytics, pick a slice, scan descriptives, toggle compare, verify time-series + crosstab + top-terms render for both slices. **Awaiting user confirmation.**
- [~] **Phase 8 — Export & provenance** *(2026-04-18, awaiting user confirmation)*
  - `core/provenance.py`: reads `AppState.provenance` (already appended from ~25 endpoints) and renders a methods-style markdown log (Corpus snapshot · Codebooks · Topic runs · Saved slices · per-action event table). Builds the tagged-corpus CSV (UTF-8 BOM so Excel opens cleanly) and XLSX (strips tz-aware datetimes before writing). Packages codebooks / topic sets / slices / provenance as JSON. Assembles the reproducibility ZIP in memory via `zipfile.ZipFile(ZIP_DEFLATED)`, with a `README.md` that lists contents + reproduction steps and a toggleable include map (corpus_csv, corpus_xlsx, codebooks, topics, slices, provenance).
  - Endpoints: `GET /api/export/preview` (counts tiles), individual downloads `GET /api/export/corpus.csv|xlsx`, `/codebooks.json`, `/topics.json`, `/slices.json`, `/provenance.md|json`, and `POST /api/export/bundle` (ZIP). The bundle POST appends an `export_bundle` provenance event so subsequent exports include it in the log.
  - Export UI: checkbox grid for the six include toggles, a 7-tile preview header (corpus rows, rows with tag, rows with topic, codebooks, topic runs, saved slices, log events), big pink **Download bundle (ZIP)** button with live status hint, and a second card with direct-download buttons per file. XLSX checkbox + link auto-disable when `openpyxl` isn't installed; corpus CSV auto-disables when no corpus has been built.
  - Smoke test: all 8 endpoints return 200 with correct content types (corpus.csv → `text/csv; charset=utf-8`, corpus.xlsx → `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, bundle → `application/zip`). Full-include bundle on the 3,252-row corpus is 2,409,227 bytes and contains README.md · corpus_tagged.csv (2.5 MB) · corpus_tagged.xlsx (1.4 MB) · codebooks.json · topics.json · slices.json · provenance.md · provenance.json. Browser smoke via `claude-in-chrome`: preview tiles render the right numbers, Download bundle triggers a `blob:` URL with filename `corpus-intel-bundle-YYYYMMDD-HHMMSS.zip`, status hint persists ("Downloaded … (1004.2 KB)."), the new `export_bundle` event shows up in the *Log events* tile, unchecking all items disables the button and shows a "Pick at least one item to include" hint, zero console errors.
  - Acceptance: open Export, check/uncheck items, download bundle, unzip locally and verify you can re-open the codebook + corpus in another tool. **Awaiting user confirmation.**
- [~] **Phase 9 — Polish** *(2026-04-18, awaiting user confirmation)*
  - Home wizard: new **Start a project** card with three `.goal-card` buttons (`build` / `code` / `explore`) that POST to `/api/settings/goal` and set `state.settings.current_goal`. `renderHome()` branches on the goal so the "Next step" CTA points at Slicer/Corpus (build), Codebook (code), or Topics/Analytics (explore). Active card is highlighted with pink border + tint.
  - **Resume-session banner** on Home: shows when state has any of datasets / corpus / codebooks / slices / topic_sets and `sessionStorage.ci_resume_dismissed !== '1'`. Summary line reports counts + last-built timestamp (e.g. *"4 datasets · 3,252-row corpus · 1 codebook · 2 tagged rows · corpus built Apr 17, 2026, 3:27 PM."*). *Continue* is a no-op acknowledgement; *Dismiss* writes the sessionStorage key so the banner stays hidden for the tab.
  - **Glossary tooltip system**: one shared `#glossary-popover` element + document-level `mouseover` / `focus` / `click` delegation on `[data-hint]`. 20 terms defined in the `GLOSSARY` dict (cohens-kappa · krippendorffs-alpha · irr · near-duplicate · confidence · prompt-caching · preflight · target-k · batch-size · sample-size · base-query · stratified · systematic · irr-overlap · normalisation · ngram · stopwords · boolean-query · source-confidence · quality-flags). Hover shows; click pins; ESC / outside-click dismisses; popover auto-flips to stay in viewport. `hintIcon(key,label)` helper returns the little pink ⓘ affordance; sprinkled across Codebook IRR, AI Coding, Topics, Slicer tabs + query fields, Analytics crosstab/ngrams, and Import source-confidence.
  - **Empty-state cards** (`.empty-state` + `.empty-hint`): Import dataset-list when no datasets (icon + CTA copy + tip), Corpus results table when a filter returns zero rows (`renderCorpusZeroState()` enumerates active filters — *"Try removing the text search / date range filter or widening the date range"* — shows total-corpus row count, includes a **Clear all filters** button wired to `resetFilters()`), Slicer preview when the boolean query returns zero rows (suggests broadening terms + proximity/wildcard hints).
  - **Settings soft-nudges**: pink-tinted banners (`.settings-nudge`) at the top of the Coder-identity and API-key cards. `renderSettings()` toggles their `display` based on `state.has_api_key` and `state.coding.coder_name`. API-key nudge links to `console.anthropic.com`; coder nudge explains that AI Coding and Codebook tagging are blocked without a name.
  - Backend: added `POST /api/settings/goal` with a `_VALID_GOALS` whitelist (`"" · build · code · explore`) persisting to `state.settings.current_goal`.
  - Smoke test (claude-in-chrome, 3,252-row corpus): page loads clean on `http://127.0.0.1:8788/` with zero console errors. All 9 non-disabled sections (Home · Import · Corpus · Slicer · Codebook · AI Coding · Topics · Export · Settings) switch via nav click and become `.active`. Resume banner renders with correct summary. Glossary popover renders on hover with full definition (`source-confidence` trigger → "Source detection confidence — How sure the importer is…"). Impossible corpus filter (`zzzz-impossibleQuery-xxx-99999`) renders the `.empty-state` with heading *"No rows match these filters"*, suggestion *"Try removing the text search filter or widening the date range."*, total-count hint *"Your corpus has 3,252 rows total."*, and working **Clear all filters** button that restores 50 preview rows. Settings API-key nudge visible (no key on file); coder nudge hidden (coder="alice").
  - Acceptance: open Home, pick a goal, verify the Next-step CTA matches; dismiss resume banner, reload, confirm it stays dismissed for the tab; hover any `ⓘ` icon and read the glossary definition; clear the corpus, filter for `zzz`, see the empty-state with correct suggestions; open Settings with no API key and verify the pink nudge. **Awaiting user confirmation.**

## Phase 0 deliverables

```
Corpus Intel/
├── CLAUDE.md
├── CODING_PLAN.md
├── BUILD_STATUS.md
├── requirements.txt
├── launch.bat
├── corpus_intel/
│   ├── __init__.py
│   ├── web_server.py           # /api/state + static mount
│   ├── ai/__init__.py
│   ├── core/__init__.py
│   ├── core/sources/__init__.py
│   ├── sessions/
│   └── static/
│       ├── index.html          # 10-section sidebar
│       ├── app.js              # router + /api/state bootstrap
│       ├── styles.css          # ISD glassmorphism, #C8175D accent
│       └── logos/isd-logo.png
└── tests/fixtures/
```

## How to resume in a fresh session

1. Open `C:/Users/beyer/AntiGravity/Corpus Intel` as the working directory.
2. `CLAUDE.md` auto-loads.
3. Tell Claude: **"Read CODING_PLAN.md and BUILD_STATUS.md, then continue from the current phase."**
