"""Didactical layer (P15) — decision cards, journey log, interpretation hints.

The design goal: turn Corpus Intel into a tool you *learn methods from* while
using, not just a tool that produces outputs. Every major action has a Decision
Card (what is this step for? what does picking X vs Y mean?), every run leaves
a Journey Log entry (what you did, when, with what params, and the reasoning),
and every output surfaces Interpretation Hints (what this number does / does
not mean).

All content is in code — versioned with the app, searchable, and cheap to ship.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from corpus_intel.constants import SESSION_DIR

log = logging.getLogger("corpus_intel.didactics")

_JOURNEY_PATH = os.path.join(SESSION_DIR, "journey_log.jsonl")
_DECISIONS_PATH = os.path.join(SESSION_DIR, "decisions_registry.jsonl")
_lock = threading.Lock()


# ─── Decision Cards ─────────────────────────────────────────────────────────
# Keyed by action id — what the UI shows before a user confirms something.
DECISION_CARDS: Dict[str, Dict[str, Any]] = {
    "dedupe_near": {
        "title": "Near-duplicate removal",
        "why": "Near-duplicates (80%+ text similarity) make one narrative look like many posts, skewing topic counts and time trends.",
        "tradeoffs": [
            "ON: counts reflect unique narratives — recommended for discourse analysis.",
            "OFF: counts reflect raw posting volume — relevant for studying virality itself.",
        ],
        "reversible": True,
        "cost": "free (pure Python)",
    },
    "sample_mode": {
        "title": "Sample vs. full corpus",
        "why": "Classifying a representative sample gives you solid evidence for a fraction of the cost and time.",
        "tradeoffs": [
            "SAMPLE: fast, cheap, most questions answerable. Default.",
            "FULL: use when reporting absolute counts or filtering for rare events.",
        ],
        "reversible": True,
        "cost": "sample ≈ $0.30 · full ≈ $5+ (varies by size)",
    },
    "topic_induce": {
        "title": "Topic induction",
        "why": "Let Claude find natural themes before you commit to a codebook — avoids forcing your prior hypotheses onto the data.",
        "tradeoffs": [
            "PRO: surfaces themes you didn't expect.",
            "CON: fewer guarantees on semantic stability — always name + merge topics yourself.",
        ],
        "reversible": True,
        "cost": "≈ $0.20–$1 per run",
    },
    "ai_classify_full": {
        "title": "Classify full corpus with AI",
        "why": "Once your sample reliability is acceptable, scale to the whole corpus.",
        "tradeoffs": [
            "Reuses cached labels for unchanged rows — free repeat runs.",
            "Budget ceiling blocks at 100 % of your monthly cap.",
        ],
        "reversible": True,
        "cost": "preflight estimate shown before run",
    },
    "snapshot_switch": {
        "title": "Switch active snapshot",
        "why": "Snapshots are immutable — switching between them is safe and leaves your analyses reproducible.",
        "tradeoffs": [
            "All downstream views re-load from the new snapshot.",
            "Cached coding/topic results for one snapshot don't transfer to another.",
        ],
        "reversible": True,
        "cost": "free",
    },
    "delete_snapshot": {
        "title": "Delete a snapshot",
        "why": "Removes the parquet file and deregisters it — cannot be undone.",
        "tradeoffs": [
            "Frees disk.",
            "Loses any analysis uniquely tied to that snapshot.",
        ],
        "reversible": False,
        "cost": "free",
    },
    "enrichment_stance": {
        "title": "Stance classification",
        "why": "Stance labels show whether authors are for/against a target topic — useful for polarization studies.",
        "tradeoffs": [
            "Target must be a clear concept (policy, person, product).",
            "Unclear labels are normal — treat them as missing, not neutral.",
        ],
        "reversible": True,
        "cost": "≈ $0.40 per 1k rows",
    },
    "budget_override": {
        "title": "Override the budget ceiling",
        "why": "The cap is there to keep costs predictable — overriding means you accept the spend.",
        "tradeoffs": [
            "You must type the exact remaining amount to confirm.",
            "The override applies to THIS run only, not to future runs.",
        ],
        "reversible": False,
        "cost": "whatever the preflight says",
    },
    "clear_corpus": {
        "title": "Clear the merged corpus",
        "why": "Removes the combined working corpus. Your uploaded datasets and snapshots stay intact — only the active merge is discarded.",
        "tradeoffs": [
            "You can rebuild from the same datasets at any time.",
            "Any in-flight filters, slicer previews, or unsaved tagging on the active merge are dropped.",
        ],
        "reversible": True,
        "cost": "free",
    },
    "rebuild_corpus": {
        "title": "Rebuild the corpus",
        "why": "Re-merges all selected datasets from scratch and creates a new immutable snapshot.",
        "tradeoffs": [
            "The previous snapshot remains available — you can switch back.",
            "Takes longer on big datasets; progress shows in the jobs indicator.",
        ],
        "reversible": True,
        "cost": "free (pure Python)",
    },
}


def get_decision_card(action_id: str) -> Dict[str, Any]:
    return DECISION_CARDS.get(action_id, {})


def list_decision_cards() -> List[Dict[str, Any]]:
    return [{"action_id": k, **v} for k, v in DECISION_CARDS.items()]


# ─── Interpretation Hints ───────────────────────────────────────────────────
INTERPRETATION_HINTS: Dict[str, Dict[str, Any]] = {
    "cohens_kappa": {
        "metric": "Cohen's κ (inter-coder agreement)",
        "means": "How much two coders agree beyond chance.",
        "good": "≥ 0.80",
        "meh": "0.60–0.79",
        "bad": "< 0.60 → revise your codebook",
        "caveats": [
            "Penalises severe class imbalance — a high κ on a skewed dataset is real.",
            "Only meaningful on the SAME rows coded by BOTH coders.",
        ],
    },
    "krippendorffs_alpha": {
        "metric": "Krippendorff's α",
        "means": "Generalisation of κ — handles >2 coders and missing data.",
        "good": "≥ 0.80",
        "meh": "0.667–0.79",
        "bad": "< 0.667 → do not publish",
        "caveats": [
            "Requires overlap — each row must be coded by at least two coders.",
        ],
    },
    "tv_distance_sample": {
        "metric": "Total-variation distance (sample vs. corpus)",
        "means": "How far a sample's distribution drifts from the full corpus.",
        "good": "≤ 0.05 — sample is near-identical",
        "meh": "0.05–0.15 — usable but note the drift",
        "bad": "> 0.15 — stratify or re-sample",
        "caveats": ["Per-dimension; check each (platform, language, date, etc.) separately."],
    },
    "ai_confidence": {
        "metric": "AI classifier confidence",
        "means": "The model's self-reported certainty for this label.",
        "good": "≥ 0.8",
        "meh": "0.6–0.8 — worth spot-checking",
        "bad": "< 0.6 — route to human coding via 'uncertain' queue",
        "caveats": [
            "Confidence is not calibrated — use as a ranking signal, not absolute probability.",
            "Compare confidence distributions across labels — anomalies usually mean codebook drift.",
        ],
    },
    "topic_size": {
        "metric": "Topic size",
        "means": "Share of posts assigned to this topic.",
        "good": "Most topics carry 5–20 % of the corpus.",
        "meh": "One topic >50 % → probably under-specified; split it.",
        "bad": "Dozens of 1 %-topics → over-specified; merge similar ones.",
        "caveats": ["Post count ≠ importance — small topics can dominate certain time windows."],
    },
    "engagement_top_decile": {
        "metric": "Top-decile engagement share",
        "means": "What fraction of engagement comes from the top 10 % of posts.",
        "good": "< 60 % — broadly distributed",
        "meh": "60–85 % — typical for social platforms",
        "bad": "> 85 % → a handful of posts drive the narrative; investigate authorship",
        "caveats": ["Bot activity often produces extreme skews."],
    },
    "exact_id_dupes_removed": {
        "metric": "Exact ID duplicates removed",
        "means": "Rows dropped because another row already had the same post_id. These are literal repeats of the same post (usually from overlapping exports or a re-pull), not near-duplicates.",
        "good": "Small or zero — sources were clean.",
        "meh": "5–20 % of input — normal for overlapping exports, worth noting in methods.",
        "bad": "> 20 % → a source was exported twice; reopen the Import step.",
        "caveats": ["Only applies when a post_id column was mapped. Without it, only text-based dedup runs."],
    },
    "near_duplicates_removed": {
        "metric": "Near-duplicates removed",
        "means": "Rows dropped because their normalised text was identical to another row (lowercase, trimmed whitespace, stripped URLs). Catches copy-paste reposts, cross-posts and template messages.",
        "good": "0–5 % of input — clean, distinct posts.",
        "meh": "5–25 % — expected on activist campaigns, official statements, news pickups.",
        "bad": "> 25 % → your corpus is mostly the same narrative repeated; document this and decide whether to count unique or raw.",
        "caveats": [
            "Only fuzz-identical (after normalisation) rows are merged — paraphrases survive.",
            "Turn OFF if you're studying virality itself, where repetition IS the signal.",
        ],
    },
    "rows_total": {
        "metric": "Final row count",
        "means": "How many rows survived deduplication and made it into the working corpus. This is the denominator for every downstream analysis.",
        "good": "At least a few hundred per category you want to compare — bigger is better for trends.",
        "meh": "Under ~200 — treat findings as qualitative, not statistical.",
        "bad": "Under ~50 → cite as a pilot, not a study.",
        "caveats": ["This count applies to the snapshot that's currently active. Rebuilding or switching snapshots changes it."],
    },
    "input_rows": {
        "metric": "Rows in (before dedup)",
        "means": "Sum of all rows across the selected datasets before any duplicate removal. The difference between this and Final rows tells you how much the dedup step pruned.",
        "good": "—",
        "meh": "—",
        "bad": "—",
        "caveats": ["Changes whenever you add or remove a source dataset and rebuild the corpus."],
    },
    "rows_tagged": {
        "metric": "Rows with at least one tag",
        "means": "Rows where any coder (human or AI) has applied at least one codebook category. Required for IRR and for most downstream analytics that depend on codes.",
        "good": "Approach 100 % of the corpus once coding is complete — or 100 % of the sample you intend to analyse.",
        "meh": "The ratio to total rows tells you how far along you are.",
        "bad": "0 % after a run → something blocked tagging (no active codebook, no coder name, API key missing).",
        "caveats": ["A single tag counts — the row may still have other categories untagged."],
    },
    "topic_other_share": {
        "metric": "'Other' bucket share",
        "means": "Fraction of rows the classifier couldn't fit into any named topic — rows go here when no topic scored above the assignment threshold.",
        "good": "< 10 % — your topic set covers the corpus well.",
        "meh": "10–25 % — consider adding a topic or broadening an existing one.",
        "bad": "> 25 % → the topic set is missing a major theme. Spot-check the Other rows and induce again.",
        "caveats": ["A large Other can also mean the corpus is genuinely diverse — check the examples before splitting."],
    },
    "tagged_share": {
        "metric": "Coded share of corpus",
        "means": "Fraction of rows that carry at least one code in the active codebook. Drives every per-category % you see in Analytics.",
        "good": "≥ 80 % of the intended scope.",
        "meh": "50–80 % — partial coverage, be careful with absolute counts.",
        "bad": "< 50 % → findings will be biased toward the coded portion.",
        "caveats": ["Scope matters — a 'coded share' over a small slice can be high while the whole corpus remains mostly uncoded."],
    },
    "slice_rows": {
        "metric": "Rows in this slice",
        "means": "How many rows from the active corpus match this saved slice's boolean query (or sampled selection).",
        "good": "Enough rows for the question you're asking — typically ≥ 100.",
        "meh": "20–100 — qualitative claims only.",
        "bad": "< 20 → either too narrow or the query has a typo.",
        "caveats": ["Slice results are live — if the corpus is rebuilt they are re-evaluated on the new snapshot."],
    },
    "cache_hit_rate": {
        "metric": "AI answer-cache hit rate",
        "means": "Share of rows served by the on-disk answer cache at $0, instead of a fresh API call. Determined by (row hash + codebook version + prompt version).",
        "good": "≥ 80 % on repeated runs — means the cache is doing its job.",
        "meh": "30–80 % — codebook edits naturally invalidate rows; the cache will warm back up.",
        "bad": "< 30 % on a re-run → something changed upstream (codebook, prompt, model).",
        "caveats": ["Cache hits always count, but don't show in preflight cost."],
    },
    "prompt_hint": {
        "metric": "Prompt hint (AI-facing description)",
        "means": "The short sentence the AI reads when deciding whether a row belongs in this category. Written as an instruction to a careful research assistant: include inclusion rules, edge cases, and what to exclude.",
        "good": "One or two crisp sentences, one rule per sentence.",
        "meh": "Copy of the human-facing title — the AI will guess.",
        "bad": "Empty — classifier has nothing to anchor on.",
        "caveats": ["Use the same wording you'd use to train a human coder. The AI follows it literally."],
    },
    "exclusion_group": {
        "metric": "Exclusion group",
        "means": "Two categories in the same exclusion group cannot be applied to the same row by the same coder — the newer tag replaces the older one. Use it when categories are mutually exclusive (e.g. pro / anti / neutral).",
        "good": "One group name for each set of mutually exclusive choices.",
        "meh": "—",
        "bad": "Everything in one group → you've collapsed the codebook into a single-select.",
        "caveats": ["Categories in different groups are independent — a row can carry one tag from each."],
    },
    "coder_identity": {
        "metric": "Coder identity",
        "means": "The display name attached to every tag you place. Required before you can tag rows, since every IRR calculation and every provenance entry is keyed on it.",
        "good": "A short, stable identifier — your initials or first name works.",
        "meh": "—",
        "bad": "Empty → tagging is blocked and IRR cannot be computed.",
        "caveats": ["Changing your name mid-project creates two apparent coders. Pick one name and keep it."],
    },
    "ai_pill": {
        "metric": "AI status",
        "means": "Lives in the header on every page. Green = Claude is reachable AND a key is on file. Yellow = API reachable but no key. Red = unreachable (network, outage, or bad key).",
        "good": "Ready — AI Coding, Topics, and AI codebook suggestions are unlocked.",
        "meh": "No key — click the pill to open Settings and paste one.",
        "bad": "Offline → everything non-AI still works; AI runs will be blocked.",
        "caveats": ["Refreshes automatically every 60 seconds, or click the pill to re-check now."],
    },
    "budget_pill": {
        "metric": "Budget status",
        "means": "Lives in the header on every page. Shows how much you've spent on Anthropic calls this month against your ceiling. Click to open Settings and change the cap.",
        "good": "< 80 % used — AI runs proceed normally.",
        "meh": "80–100 % — preflight shows a warning; runs still go through.",
        "bad": "100 % → preflight blocks the run unless you type the exact remaining amount.",
        "caveats": ["Set the cap to 0 to disable the ceiling entirely."],
    },
}


def hint_for(metric_id: str) -> Dict[str, Any]:
    return INTERPRETATION_HINTS.get(metric_id, {})


def list_hints() -> List[Dict[str, Any]]:
    return [{"metric_id": k, **v} for k, v in INTERPRETATION_HINTS.items()]


# ─── Journey Log (append-only record of what the user did, when, why) ──────
def journey_append(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Append an entry to the journey log. Entry should have: action, params, notes."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    full = {"ts": ts, **entry}
    with _lock:
        os.makedirs(os.path.dirname(_JOURNEY_PATH), exist_ok=True)
        with open(_JOURNEY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(full, ensure_ascii=False, default=str) + "\n")
    return full


def journey_tail(limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(_JOURNEY_PATH):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(_JOURNEY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out[-limit:]


# ─── Decisions Registry (durable "why" log, curated not auto-generated) ────
def register_decision(action_id: str, *, choice: str, reason: str = "",
                      coder: str = "") -> Dict[str, Any]:
    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "action_id": action_id,
        "choice": choice,
        "reason": reason,
        "coder": coder,
    }
    with _lock:
        os.makedirs(os.path.dirname(_DECISIONS_PATH), exist_ok=True)
        with open(_DECISIONS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def decisions_registry(limit: int = 500) -> List[Dict[str, Any]]:
    if not os.path.exists(_DECISIONS_PATH):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(_DECISIONS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out[-limit:]


# ─── Tour scripts ───────────────────────────────────────────────────────────
TOURS: Dict[str, List[Dict[str, Any]]] = {
    "first_time": [
        {"page": "home", "anchor": None, "title": "Welcome to Corpus Intel",
         "body": "A research workstation for social-media corpora. In five minutes you'll have a searchable, coded, and analyzable dataset."},
        {"page": "import", "anchor": "#btn-upload", "title": "Step 1 · Import",
         "body": "Upload a CSV, Excel, JSON, or JSONL. Corpus Intel auto-detects the data source and proposes a column mapping you'll review."},
        {"page": "corpus", "anchor": "#btn-build-corpus", "title": "Step 2 · Build",
         "body": "Combine your datasets into a working corpus. Dedup options are explained in Decision Cards."},
        {"page": "coding", "anchor": None, "title": "Step 3 · Code",
         "body": "Create a codebook and label a sample — by hand, by AI, or both. Cohen's κ tells you if they agree."},
        {"page": "topics", "anchor": None, "title": "Step 4 · Discover",
         "body": "Let Claude induce topics on a sample. Name them. Classify the rest."},
        {"page": "analytics", "anchor": None, "title": "Step 5 · Analyse",
         "body": "Trends, crosstabs, network graphs. All pure Python — no further AI cost."},
        {"page": "report", "anchor": None, "title": "Step 6 · Report",
         "body": "Generate a Markdown / HTML report with tables, figures, and an AI-drafted abstract."},
    ],
    "modes": [
        {"page": "home", "anchor": None, "title": "Learner mode",
         "body": "Every action shows a Decision Card explaining why. Use this while you're learning a method."},
        {"page": "home", "anchor": None, "title": "Expert mode",
         "body": "Cards collapsed by default. Shortcuts on. Use this once you've done it three times."},
    ],
}


# Aliases — accept common synonyms users may type.
_TOUR_ALIASES = {
    "getting_started": "first_time",
    "intro": "first_time",
    "welcome": "first_time",
    "onboarding": "first_time",
    "mode": "modes",
}


def get_tour(tour_id: str) -> List[Dict[str, Any]]:
    return TOURS.get(_TOUR_ALIASES.get(tour_id, tour_id), [])


def list_tours() -> List[str]:
    return list(TOURS.keys())


# ─── Duplicate clusters (explorer for dedupe audit) ─────────────────────────
def cluster_duplicates(df, *, text_col: str = "text", threshold: int = 80) -> List[Dict[str, Any]]:
    """Group rows into near-duplicate clusters. Returns a list of {cluster_id, rows}."""
    try:
        from rapidfuzz import fuzz  # type: ignore
    except ImportError:
        return [{"cluster_id": 0, "note": "rapidfuzz not installed — install for duplicate-cluster explorer"}]
    if df is None or df.empty or text_col not in df.columns:
        return []
    texts = df[text_col].fillna("").astype(str).tolist()
    n = len(texts)
    # O(n^2) — fine for a few thousand; beyond that, use a blocking strategy.
    if n > 3000:
        return [{"cluster_id": 0, "note": f"Corpus too large ({n} rows) for naive clustering. Run after filtering."}]
    parent = list(range(n))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj: parent[ri] = rj
    for i in range(n):
        for j in range(i+1, n):
            if fuzz.ratio(texts[i], texts[j]) >= threshold:
                union(i, j)
    clusters: Dict[int, List[int]] = {}
    for i in range(n):
        r = find(i)
        clusters.setdefault(r, []).append(i)
    out: List[Dict[str, Any]] = []
    for cid, idxs in clusters.items():
        if len(idxs) < 2:
            continue
        out.append({
            "cluster_id": cid,
            "size": len(idxs),
            "sample_rows": [{"i": i, "text": texts[i][:300]} for i in idxs[:5]],
        })
    out.sort(key=lambda c: -c["size"])
    return out[:50]
