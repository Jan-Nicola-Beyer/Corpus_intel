# Corpus Intel — Single-User Roadmap (Phases 10–14)

> **Scope:** extend the current single-user local app into *the* tool a non-expert
> researcher uses for all of their social-media analysis work. Multi-user, auth,
> hosted SaaS, and cloud DBs are explicitly out of scope — that is a different
> product and a different engineering problem.

---

## 1. Audit — what blocks the "one app for everything" claim

Organised by the question a researcher asks when they hit the wall.

### 1.1 "Can I trust what I'm producing?" (Rigor)
- **No corpus versioning.** Rebuilding the corpus silently invalidates slices,
  tags, AI runs, topics. No way to run experiment A and experiment B on different
  snapshots side-by-side.
- **No IRR conflict-resolution UI.** The app shows κ but not *which* rows the coders
  disagreed on. The disagreement *is* the interesting data.
- **Codebook versioning is implicit.** Editing a category invalidates the answer
  cache but does not tell the user "you just changed what 83 of your existing tags
  mean."
- **No active learning.** The user pre-samples 200 rows and codes them; the app
  cannot say "code *these* 50 instead — these are the ones AI is least sure
  about."
- **No sample-diagnostic tools.** "Is 200 rows enough?" is unanswered. Saturation
  curves, representativeness checks: missing.
- **No uncertainty bars** on analytics. Point estimates without confidence
  intervals are how researchers lose credibility.

### 1.2 "Can I analyse what I actually have?" (Breadth)
- **Span-level annotation missing.** Row-level tags cannot capture *"the slur is
  in these two words."*
- **No named-entity / sentiment / stance enrichment.** Haiku does this cheaply;
  the app leaves the value on the table.
- **No network analysis.** Social-media data is relational — reply graphs,
  retweet cascades, mention networks. None of it is modelled.
- **No multimodal.** Image and video posts ship with URLs we never look at;
  Claude Vision would extract OCR + description for pennies.
- **No semantic search.** "Find rows like this one" is the single most-requested
  feature in every qualitative-coding tool. Embedding-backed search is missing.
- **No cross-lingual unification.** Multi-language corpora have no shared unit
  for analytics.

### 1.3 "Can I handle my actual data?" (Scale & stability)
- **Single-process pandas in RAM.** Breaks above ~1M rows on a laptop.
- **Long operations block the UI.** No progress, no cancel. Merges, dedupes,
  AI runs all stall the page.
- **Non-virtualised tables.** 10k rows already sluggish; 100k will freeze the
  browser.
- **One session file, no rotation.** A crash mid-write → total loss.
- **No graceful degradation.** API down → app feels broken.
- **No cost ceiling.** One accidental "All rows" click on a 500k corpus is a
  real-money incident.
- **Known bug (see memory):** Suggested-sources button does not appear after
  analysis despite working endpoint. Fix as part of Phase 10.

### 1.4 "Can I tell my story?" (Reporting)
- **No methods-section generator.** The provenance markdown is raw truth, not
  publishable prose.
- **No figure export at publication resolution.**
- **No quote extractor** — "pick me 3 representative posts per topic" is manual.
- **No APA / LaTeX table formatting.**

### 1.5 "Can I work without reading documentation?" (Onboarding)
- **No guided tour.** Home tells you what the app *is*; it doesn't walk you
  through using it.
- **No demo dataset.** "Upload a CSV to get started" is a wall for a first-time
  user.
- **No recipe pages.** "I want to find hate speech" is a reading exercise, not
  a guided path.
- **Undo is tagging-only.** Everywhere else the researcher is one mis-click away
  from losing work.

---

## 2. What we keep

Every Phase 10–14 deliverable is additive. We do not rewrite the existing
pipeline. The following already work and are load-bearing for the plan:

- `AppState` + session-file persistence
- `ProvenanceEvent` log (~25 endpoints already append)
- Answer cache with version-keyed invalidation
- Mandatory preflight + prompt caching for every AI run
- Cohen's κ + Krippendorff's α implementations
- Glossary tooltip system (20 terms)
- Boolean-query parser + sampling primitives (random / stratified / top / systematic / split with IRR overlap)
- Export bundle with README + provenance markdown + per-artefact JSON

---

## 3. Design principles for the upgrade

1. **Extend, don't rewrite.** Every new capability ships behind an additive
   façade so the existing callers keep working.
2. **DuckDB for scale.** Embedded, single-file, reads pandas + Parquet + JSON
   natively, runs SQL. Replaces pandas-in-RAM when `rows > 50k`. Not a database
   in the "we need a server" sense; it's a pandas accelerator.
3. **Immutable corpus snapshots.** The corpus becomes content-addressed
   (Parquet + manifest). Rebuilds create a new snapshot; they do not overwrite.
   Slices, tags, topics pin to a `snapshot_id`.
4. **Background jobs with progress.** Any operation > 1 s becomes a job with
   SSE progress + cancel. No more blocked UI.
5. **Rigor before cosmetics.** Fix "can I trust this" before adding UMAP.
6. **Every new AI feature is preflight-gated and cached** — no exceptions.

---

## 4. Phases

### Phase 10 — Foundations (stability & scale) — ~2 weeks

**Goal:** the app does not break under real research loads.

Deliverables:
1. `core/corpus_store.py` — DuckDB-backed façade. Auto-routes: `≤50k rows`
   → pandas (as today); `>50k` → DuckDB on a per-snapshot Parquet file in
   `sessions/corpora/{snapshot_id}.parquet`. Existing callers get pandas
   semantics.
2. **Immutable corpus snapshots.** `/api/corpus/merge` writes a new
   content-addressed snapshot (hash of dataset IDs + dedupe params + mapping
   versions). `state.corpus.active_snapshot_id` points at the current one.
   Slices / tags / topic-runs carry `snapshot_id`. Snapshots API:
   list / activate / delete. Frontend: snapshot picker in the Corpus header.
3. **Background-job system.** In-process worker (no Redis). Jobs: merge,
   dedupe, AI-classify, topic-induce, topic-classify, analytics-export,
   embedding-index-build. `GET /api/jobs/{id}/events` streams SSE progress.
   Every long-op UI gains a progress bar + cancel button.
4. **Virtualised tables.** Swap Corpus + Slicer + Coding tables to
   TanStack Virtual (or equivalent). Target: render time O(viewport),
   not O(total-rows).
5. **Streaming upload.** Chunked parse; never load >200 MB of CSV into RAM.
   Show per-chunk progress.
6. **Session-file rotation.** `sessions/history/latest-{ts}.ci`, keep last 10.
   On corrupt load, auto-recover to last-good + loud toast.
7. **Persistent undo log.** `sessions/undo.jsonl` — append-only. Survives
   reload. Rolling cap (last 500 actions).
8. **Cost ceiling.** Settings → monthly budget (USD). Preflight estimates
   subtract from remaining budget; 80 % → warn, 100 % → hard-block with
   override-requires-typing-the-number.
9. **Graceful degradation.** If `api.anthropic.com` unreachable, the app
   still lets the user do corpus-build, manual coding, analytics, export.
   AI features show a banner, not a broken page.
10. **Fix the suggested-sources bug.** Track down why the button doesn't
    appear; add a regression test.

**Acceptance:** synthetic 2 M-row corpus — upload, build, slice, tag 200 rows
via AI, export — UI remains responsive, state file stays under 10 MB,
power-cycle mid-merge recovers cleanly.

---

### Phase 11 — Research rigor — ~2 weeks

**Goal:** the output is defensible in a peer-reviewed paper.

Deliverables:
1. **IRR conflict-resolution UI** (new panel under Codebook). Rows where
   two or more coders tagged differently, side-by-side. Actions per row:
   pick winner · create gold tag · split into new category · mark as
   unresolvable. Gold tags feed a re-computed "reconciled κ/α" score.
2. **Codebook version history.** Every edit writes a diff entry
   (`add`, `remove`, `rename`, `merge_cats`, `change_exclusion_group`)
   with timestamp + coder. Existing tags freeze the codebook version they
   were placed under. Rename flow asks: "migrate existing tags?" with a
   preview of affected rows.
3. **Active-learning loop.** After an AI run, "Next 50 rows to code"
   surfaces rows ranked by model uncertainty (entropy over category
   probs + exclusion-group ambiguity). Feeds the next manual-coding
   sample. Loop stops automatically when uncertainty plateau detected.
4. **Methodological guardrails.** Rule engine in `core/guardrails.py`.
   Examples:
    - κ < 0.6 → "Agreement is low. Consider a reconciliation session on
      a 50-row overlap sample before scaling to the full corpus."
    - `sample_size / corpus_size < 0.02` on a κ < 0.8 codebook →
      "Sample too small for unstable codebook — increase to 400+."
    - Topic induction k=8 returned only 4 → "Sample may be homogeneous —
      try larger sample or different scope."
   Shown inline at the decision point, not as a generic tooltip.
5. **Sample diagnostics.** Saturation curve for topics (do new topics
   still emerge as sample grows?). χ² representativeness check of any
   sample against the full corpus on platform / date / language. Auto-run
   when the user draws a sample or induces topics.
6. **Span-level annotation.** Data-model generalisation: tag entries gain
   optional `[start, end]` offsets into text. Manual coding UI: select
   text → assign category (fallback: whole row). Span-aware export:
   CSV keeps row-level tags; JSON export gains a `spans` field.
   AI coding: a second pass (opt-in, Haiku) returns the offending span
   per tagged row. Existing row-level tags continue to work unchanged.

**Acceptance:** two-coder end-to-end IRR workflow — split corpus with
overlap, both code, conflict-resolve, produce gold-standard κ on the
resolved set. Span tags round-trip cleanly through export/re-import.

---

### Phase 12 — Analytical breadth — ~2 weeks

**Goal:** one app replaces Gephi + a Python notebook + a semantic-search tool.

Deliverables:
1. **Semantic search + "find rows like this."** Voyage-multilingual-2
   embeddings via Anthropic on corpus text. Index stored in-process via
   DuckDB's HNSW extension. New Slicer tab: "Rows similar to…" — accepts
   a text query or a seed row ID. Returns top-K with cosine score.
   Works as a slice source.
2. **UMAP topic scatter.** Every classified topic run gets a 2D UMAP
   projection (Plotly). Hover = row text; lasso-select = create slice
   from selection. Colour by topic by default; swappable to any
   categorical column.
3. **Enrichment passes (optional, preflighted).** Haiku-backed synthetic
   columns: `entities` (persons / orgs / locations, JSON list),
   `sentiment` (neg/neu/pos + score), `stance` (pro/against/neutral on
   a user-supplied target). Cached per-row. Appear as first-class
   dimensions in Analytics.
4. **Network analysis.** When canonical schema has reply/mention/retweet
   edges, new **Network** section. Force-directed graph (Cytoscape.js),
   Louvain community detection, centrality metrics (degree, betweenness,
   eigenvector). New slice kind: "posts by top-K users in community X."
   CSV export of edge list.
5. **Multimodal (lite).** For rows with image URLs: optional Claude
   Vision pass extracts OCR text + image description into synthetic
   columns `image_ocr`, `image_description`. Cached per-URL-hash.
   Video: require transcript in the upload (punt on STT).
6. **Cross-lingual unification.** Optional Haiku pass produces `text_en`
   synthetic column. Analytics can toggle "use translated text" so
   n-grams / topics / semantic search work across a multilingual corpus.

**Acceptance:** multi-platform corpus with images and a reply graph
produces a topic scatter, a force-directed network with community
labels, and a semantically-similar slice — all from inside the app.

---

### Phase 13 — Reporting & publishing — ~1 week

**Goal:** the researcher never leaves the app to draft methods + findings.

Deliverables:
1. **Report generator.** New Export action. Sonnet reads provenance +
   corpus summary + stats + quote extractor output and drafts:
   (a) **Methods section** — academic tone, IMRaD-compatible, cites
   every parameter choice by referencing the provenance log;
   (b) **Findings summary** — 3–5 paragraphs per topic / codebook
   category with representative quotes;
   (c) **Limitations** — auto-populated from guardrails warnings the
   user hit during the project.
   Output is Markdown; user edits in-app before export.
2. **Publication-resolution figures.** Every chart gains 300-dpi PNG +
   SVG download. Bundled under `/figures/` in the export ZIP.
3. **Quote extractor.** For each category + each topic: Sonnet picks
   3–5 representative rows with one-line justification. Cached by
   `(category_id | topic_id, snapshot_id)`. Exported under `/quotes/`.
4. **Table exports.** Descriptives + cross-tabs export as APA,
   Markdown, LaTeX (beyond the existing CSV).

**Acceptance:** a single export ZIP contains everything needed to draft
a short research paper — tagged corpus, figures, quotes, methods draft,
limitations, provenance — with no other tool needed.

---

### Phase 14 — Onboarding & daily polish — ~1 week

**Goal:** a cold-start researcher is productive in 15 minutes.

Deliverables:
1. **Interactive guided tour.** Bundled 500-row demo dataset (neutral
   topic — e.g. electric-vehicle discussion on Reddit / Mastodon).
   Home → "Take the tour" → one click loads the dataset, then the app
   walks through Import → Corpus → Codebook → AI Coding → Export with
   DOM-anchored coachmarks. Skippable, resumable.
2. **Recipe pages.** New Home card: "What do you want to do?" Each
   recipe (Find hate speech · Measure a moral panic · Compare two
   movements · Quantify astroturfing) chains the right sections with
   sensible defaults pre-filled and explanatory copy at each step.
3. **Contextual help sidebar.** Collapsible right-rail panel per section:
   scenario-specific tips · glossary entries for the current page ·
   "what should I click next?" hint. Replaces the scattered inline
   hints.
4. **Universal undo.** Extend undo beyond tagging: merge, slice save,
   codebook edit, topic rename, bulk tag, AI-run accept, snapshot
   activation. Ctrl+Z / Ctrl+Shift+Z work everywhere. Backed by the
   Phase 10 persistent undo log.
5. **Keyboard-first navigation.** Ctrl+K command palette (fuzzy search
   all sections + all saved slices + all codebooks). Arrow-key
   navigation in every table.

**Acceptance:** an HCI-researcher friend with no prior exposure to the
app completes the demo tour and produces a tagged-corpus export
independently in under 20 minutes.

---

## 5. What we took from the external review and what we dropped

**Kept (fits single-user scope):**
- Virtualised table rendering → Phase 10
- Background workers → Phase 10
- Span-level annotation → Phase 11
- IRR conflict-resolution UI → Phase 11
- Methodological guardrails on κ → Phase 11
- UMAP topic scatter → Phase 12
- Network analysis → Phase 12
- Multimodal (images) → Phase 12
- Guided tour with demo dataset → Phase 14

**Dropped (belong to a different product):**
- Proper database architecture (Postgres / S3)
- Authentication / RBAC / roles
- WebSocket / CRDT concurrency
- Secrets-at-rest / Vault
- Managed LLM billing

**Dropped (rules out per project policy):**
- Local / open-source LLMs (Anthropic-only stands; see CLAUDE.md)

**Added that the review didn't raise:**
- Corpus versioning / immutable snapshots (core stability primitive)
- Codebook version history + tag migration
- Active-learning loop
- Sample diagnostics + saturation curves
- Cost ceiling
- Session-file rotation + auto-recovery
- Graceful degradation when Anthropic API is down
- Report generator + quote extractor
- APA / LaTeX table export
- Recipe pages
- Universal undo

---

## 6. Total estimate

~**8 weeks** of focused single-developer work.

Critical path: **Phase 10 (Foundations)** — everything downstream relies on
the snapshot + job-system primitives. Phases 11 and 12 can run in
parallel once Phase 10 lands. Phases 13 and 14 can slot in whenever.

---

## 7. Success criterion

The **afternoon test**: a social scientist with no prior exposure to the
app, one 500 k-row export, and a real research question (e.g. "measure
the mainstreaming of conspiracy rhetoric in German-language Telegram
channels during the 2025 election cycle") can produce a methodologically
defensible tagged corpus + topic model + analytics + draft methods
section **in one afternoon**, without asking anyone for help.

If that passes, Corpus Intel is *the* app. If it doesn't, we have more
Phase 14 work to do.

---

## 8. The didactical layer — "best didactical app ever built"

The roadmap above makes Corpus Intel **rigorous and stable**. This
section makes it **teach the user what they are doing**. A non-expert
coder should finish an afternoon with the app *understanding why each
step mattered*, not just having clicked through it.

The user's guiding requirement:

> The user should never feel lost. All options should be clear. He or
> she should understand how each step may improve the analysis. He or
> she should also be in full control — e.g. are duplicates filtered
> always, or can the user decide when duplicates are relevant?

The didactical layer is not a separate module — it is a set of
**primitives** that every phase above picks up.

### 8.1 Principles

1. **Transparency over automation.** The app never silently does
   something consequential. If dedup removes 2,688 rows, the user sees
   *those* 2,688 rows first.
2. **Preview before commit.** Every destructive or shape-changing
   action (build corpus, apply filter, merge datasets, run AI coding)
   has a dry-run view first. Commit is always a deliberate second click.
3. **Every default is justified.** Defaults exist, but the UI shows
   *why* this default was chosen and *when* the other choice would be
   correct. No naked checkboxes.
4. **Reversibility is universal.** Every write goes to the undo stack.
   The user can revert any step without losing downstream work that
   doesn't depend on it.
5. **Progressive disclosure.** Three modes: Novice (guidance on),
   Standard (compact), Power-user (hotkeys, no hand-holding). Set once
   in Settings, applies everywhere.
6. **Show the tradeoff.** Filtering near-duplicates makes slopes
   cleaner. Keeping near-duplicates preserves *campaigns*. The app
   tells the user this, in context, in plain language.

### 8.2 Didactical primitives (build once, reuse everywhere)

These are the reusable components that every section of the app pulls
from. Each one is a small, stable contract.

**Decision Card** — A standard panel the UI drops next to any
consequential toggle. Fields: What this does · Default and why · When
to flip it · Example from your corpus. Used by: dedup toggles, sample
size picker, topic-K slider, IRR kappa target, outlier filter, date
range, language filter.

**Preview-before-commit Panel** — Before any action that changes the
corpus shape (build, filter apply, merge, dedupe, delete selection),
show a *diff*: rows before → rows after, with 3–10 concrete example
rows that would be removed or altered. User clicks **Commit** or
**Cancel**. Default binding: Enter = Commit only after preview has been
on screen ≥2 seconds (anti-misclick).

**Impact Diff** — For any AI run (classification, topic induction,
narrative), show what changed: how many rows got which label, how
confidence distributed, which rows flipped if this is a re-run. No
"Run and wait 8 minutes then look at a blob".

**Why-this-default popover** — Clickable info-glyph next to every
default value. One paragraph max. Example for dedup: *"Near-duplicate
detection is ON because social datasets almost always contain
pipeline-duplicated rows that will inflate counts in Analytics. Turn
it OFF if duplicates are your signal — e.g. you are studying
coordinated reposting."*

**Example-from-your-data button** — Every abstract concept (cluster,
topic, entity, span) gets a button that pulls 3 random matching rows
from the *user's* corpus. Concrete > abstract. Used by topic cards,
entity badges, IRR disagreement pairs.

**Pedagogical error messages** — No "500 internal server error". Every
failure state explains: what failed · why it probably failed · what to
try next · one-click try-this-fix where possible. For example a
malformed CSV gets: *"Row 4312 has 7 columns but your header has 6.
Usually this means a comma inside an unquoted text field. Open the
first 3 problematic rows →"*.

**Reasoning-trace for AI actions** — Every AI call exposes a tiny
"why?" link on each output. Clicking shows: the system prompt
version, which rows were used as exemplars, the model, and the raw
response. This is what makes AI outputs *auditable* to a reviewer,
not magic.

**Journey Log** — Human-narrative version of the provenance log,
written as the user works. Not event rows — sentences. *"You imported
two files (3,400 + 9,200 rows), built a corpus with near-duplicate
detection on (2,688 removed), sampled 200 rows stratified by date,
and induced 6 topics with Sonnet on 2026-04-18 14:32."* Visible
always in a collapsible drawer. Printable. Goes into the final
report as the Methods section draft.

**Decisions Registry** — A single page in Settings that lists every
choice the user has made for this project and lets them flip any of
them with the upstream consequence highlighted. *"Near-dup ON →
flipping to OFF will re-build the corpus (adds 2,688 rows) and
invalidate the current topic model."*

**Post-hoc checklist** — After each major step, a short "did you
notice…?" card. After topic induction: *"Topic 4 has only 11 rows —
consider merging or dropping. Topic 2 kappa was 0.61 — below your
target 0.7, consider clarifying codebook category X."*

**Guided-interpretation cards** — On Analytics, each chart has a
"How to read this" panel the user can expand: what the axes mean,
what an interesting pattern looks like, what *shouldn't* be
over-interpreted. Written by a methodologist once, ships forever.

**Checkpoints** — Named snapshots the user can create at any point
("before topic induction", "after IRR resolution"). Switching between
them is a one-click rollback that doesn't destroy the current state.

### 8.3 Didactical modes

Stored in Settings, one radio. Applies globally.

- **Novice** (default on first launch): Decision Cards expanded by
  default; guided tour on every new section; preview panels enforced;
  "how to read this" auto-open on first visit.
- **Standard** (default after first completed project): Decision
  Cards collapsed but one-click; previews enforced only on
  shape-changing actions; tours dismissable.
- **Power-user**: Decision Cards hidden; previews on destructive
  actions only; hotkeys surfaced; Ctrl+K command palette is primary
  navigation.

Switching is non-destructive and reversible anytime.

### 8.4 The duplicates question — worked example

Because the user raised this explicitly, duplicate handling is the
canonical demonstration of the didactical primitives working
together. It answers the question *"are duplicates filtered always,
or can I decide?"* with: **you decide, and the app teaches you how to
decide well**.

**In Import.** When the user uploads two files, a Decision Card
appears: *"Found 187 rows that appear in both files (same post ID).
By default we keep one copy. Change →"* One-click to see the 187
example rows side-by-side.

**In Corpus build.** Before the build runs, a Preview-before-commit
Panel shows:

```
Source rows:            12,600
Exact duplicates:          412  (same post ID — always removed)
Near-duplicates:         2,688  (identical text, different IDs)
  └─ Would remove:       2,688  [DEDUP ON  — default]
  └─ Would keep all:         0  [DEDUP OFF]

Impact if you keep them:
  • @topuser goes from 47 posts to 312 posts (6.6× inflation)
  • Date distribution peaks shift into a 3-day campaign burst
  • Topic model will almost certainly create a "copypasta" topic

Impact if you remove them:
  • Coordinated-reposting patterns become invisible in Analytics
  • Some legitimate retweets/shares collapse into one row

Recommendation for your question ("measuring mainstreaming"):
  DEDUP ON. Near-dups are pipeline artifacts, not signal.

Recommendation for ("measuring coordinated amplification"):
  DEDUP OFF. Near-dups *are* the signal — keep them and study them.
```

User clicks **Build with DEDUP ON** or **Build with DEDUP OFF** or
**Cancel**. No silent default.

**After build — the Receipt.** A Journey-Log entry appears: *"Built
corpus on 2026-04-18 14:27. 12,600 → 9,500 rows. Removed 412 exact
duplicates and 2,688 near-duplicates. Kept 9,500 rows."* With an
**Undo build** button valid until the next build runs.

**The Duplicate Clusters Explorer.** A new view under Corpus →
Duplicates. Each near-duplicate cluster is one row: *"31 rows,
identical text starts 'BREAKING: WEF announces…', posts span 4
accounts over 6 hours"*. The user can:

- keep the cluster collapsed into one row (default if DEDUP ON),
- expand it back into individual rows (per-cluster override),
- mark the cluster as "coordinated" (adds a tag, keeps all rows,
  flags them for network analysis later).

This means **dedup is not one binary for the whole corpus**. It is a
default with per-cluster overrides. Full control, with sensible
defaults that are always justified and always reversible.

**In Sample.** A second, independent toggle. Default OFF — a sample
drawn from an already-deduped corpus should preserve any remaining
quasi-duplicates so the researcher can see them. Decision Card
explains this.

**In Analytics.** Every chart that could be skewed by duplicate
inflation (top authors, daily volume) carries a footnote: *"Dedup ON
during corpus build. To compare with dedup OFF, create a checkpoint
and re-build."*

**In Settings → Decisions Registry.** One row: *"Corpus dedup: ON
(2026-04-18). Flipping to OFF rebuilds the corpus, invalidates the
current topic model, keeps annotations."*

This is what "full control" looks like as UX: default, justification,
preview, commit, receipt, undo, per-item override, registry, footnote.

### 8.5 Per-section didactical additions

Threading the primitives above through the existing ten sections.

**Home** — First-launch: a single "What do you want to do today?"
card with four paths (Import from scratch · Resume project · Demo
corpus · Tour the app). Second launch onward: the Journey Log
preview for the last project plus a "Next sensible step" nudge
derived from project state.

**Import** — Decision Card on every column mapping suggestion.
Preview panel on every upload (first 10 rows + column types +
detected date format + detected language).

**Corpus** — Worked above. Add: filter changes show a live row-count
diff before apply.

**Slicer** — Every save-as-slice carries a Decision Card explaining
*why* the user might want this slice separately named (it becomes a
reusable comparator in Analytics). Guided-interpretation card on the
slice preview ("You are looking at 312 rows — enough for descriptive
stats, probably not enough to train a classifier").

**Codebook** — Category-definition editor shows 3 live examples from
the corpus that *currently* match the definition and 3 that *almost*
match. Edits recompute the examples in real time. This is how a
non-expert learns what "a good definition" looks like.

**AI Coding** — Preflight cost card already exists (Phase 5). Add:
reasoning-trace on each coded row; post-run Impact Diff ("847 rows
labeled high-confidence, 153 flagged for review, 0 refused").
Post-hoc checklist: "3 rows got labels not in the codebook — review?".

**Topics** — K-slider carries a Decision Card with a coherence-vs-K
mini-chart specific to this corpus (cheap to compute). Each topic
card has an Example-from-your-data button. Post-hoc checklist flags
small/incoherent topics.

**Analytics** — Every chart has a Guided-interpretation card.
Footnotes surface earlier methodological choices that affect the
chart (dedup, language filter, date window).

**Export** — Preview panel shows exactly which columns and rows are
going out, with row-count diff. Decision Card on file format
explaining when CSV vs XLSX vs JSONL is the right choice.

**Settings** — Houses the Decisions Registry, mode picker, demo
corpus loader, tour restart, cost ceiling, provenance export.

### 8.6 Phase 15 — Didactical layer (concrete deliverables)

~**1–2 weeks** once Phases 10–14 have landed. These deliverables
implement the primitives above:

1. **`ui/decision_card.js` component** + catalog of ~25 cards covering
   every consequential default in the app. JSON-authored content so
   a methodologist can edit copy without touching JS.
2. **`ui/preview_panel.js` component** + endpoint contract
   `POST /api/<action>/preview` returning `{before, after, examples,
   tradeoffs, recommendation}` used by every shape-changing action.
3. **Journey Log writer** — a thin layer over the provenance log that
   emits sentence-level human-readable entries. Renderable inline and
   exportable as `methods.md`.
4. **Decisions Registry page** — Settings tab listing every
   user-chosen value with its consequence and a flip button guarded
   by the preview panel.
5. **Duplicate Clusters Explorer** — the per-cluster override UI
   described in 8.4, including "coordinated" tagging.
6. **Guided-interpretation cards** — JSON catalog of ~20 cards for
   Analytics charts, Topics outputs, Codebook diagnostics.
7. **Mode switcher** — Novice / Standard / Power-user toggle +
   per-component visibility wiring.
8. **Pedagogical error envelope** — middleware that turns any
   backend exception into `{what, why, tryThis, fixEndpoint?}` and a
   frontend renderer for it.
9. **Reasoning-trace drawer** — attached to every AI output row,
   showing prompt version, exemplars, model, raw response.
10. **Tour scripts** — 6 section-scoped tours (Import, Corpus,
    Codebook, AI Coding, Topics, Analytics) using the Phase 14 tour
    engine, each ending on a "Try it with the demo corpus" CTA.

### 8.7 How this threads into Phases 10–14

The didactical layer isn't Phase 15-only. Each earlier phase has
didactical acceptance criteria baked in:

- **Phase 10 (Foundations):** background-job progress UIs show *what*
  the job is doing, not just a spinner. Preview endpoints defined
  as part of the shape-changing endpoint contract from day one.
- **Phase 11 (Rigor):** IRR conflict UI *is* a didactical surface —
  it explains why this disagreement matters and suggests codebook
  edits. Active-learning loop shows the user *why* this row was
  picked next.
- **Phase 12 (Breadth):** every new method (entities, network,
  semantic search, UMAP) ships with a Decision Card and a
  Guided-interpretation card or it doesn't ship.
- **Phase 13 (Reporting):** Journey Log *is* the methods-section
  draft. APA tables include their own footnotes auto-generated from
  the Decisions Registry.
- **Phase 14 (Onboarding):** tours, demo corpus, Ctrl+K palette
  described there become the surface the didactical primitives
  appear on.

### 8.8 Success criterion for the didactical layer

A variant of the afternoon test: **the same non-expert, on the same
500k-row corpus, without reading any manual, can (a) correctly
decide whether to keep or remove near-duplicates for their stated
research question, (b) defend that choice in plain language to a
reviewer, (c) reverse it and compare both versions if asked**. If
that passes, the app is not just rigorous — it has taught.
