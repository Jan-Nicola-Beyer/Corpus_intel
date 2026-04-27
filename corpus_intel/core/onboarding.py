"""Onboarding scaffolding (P14) — demo dataset, recipes, contextual help.

All content is in code so it versions with the app.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import random
from typing import Any, Dict, List

import pandas as pd

log = logging.getLogger("corpus_intel.onboarding")

# ─── Demo dataset ────────────────────────────────────────────────────────────
_DEMO_AUTHORS = ["alice_h", "bobnet", "carolsays", "d_analyst", "eve42",
                 "frankly", "giovanna_l", "hakimi", "isabel_", "jon_reads"]
_DEMO_PLATFORMS = ["twitter", "reddit", "meta", "tiktok"]
_DEMO_LANGS = ["en", "es", "fr", "de"]
_DEMO_SEEDS = [
    ("Climate policy is finally taking shape in the EU — looking forward to real outcomes.", "en"),
    ("No way the new fuel tax is going through without protests.", "en"),
    ("La nueva ley migratoria plantea dudas sobre derechos humanos.", "es"),
    ("#GreenDeal está cambiando mercados que creíamos estables.", "es"),
    ("Les manifestations à Paris continuent ce week-end.", "fr"),
    ("Das Gesetz über digitale Dienste setzt neue Standards.", "de"),
    ("I love how the football team handled that tough match yesterday.", "en"),
    ("Public transport in this city is honestly a disaster.", "en"),
    ("Cost of living keeps rising, yet wages don't move.", "en"),
    ("Vaccination rollout in rural areas still lags behind.", "en"),
    ("Local elections this weekend — every vote matters.", "en"),
    ("Reddit mods are removing posts without explanation again.", "en"),
    ("The new AI regulation actually makes a lot of sense to me.", "en"),
    ("Media coverage of the crisis is suspiciously one-sided.", "en"),
]


def generate_demo_dataframe(*, n_rows: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed)
    start = dt.datetime(2026, 1, 1)
    rows: List[Dict[str, Any]] = []
    for i in range(n_rows):
        text, lang = rng.choice(_DEMO_SEEDS)
        if rng.random() < 0.25:
            # nudge some posts with author mention for network analysis
            text += f" @{rng.choice(_DEMO_AUTHORS)}"
        if rng.random() < 0.2:
            text += " #research"
        rows.append({
            "post_id": f"demo_{i:06d}",
            "posted_at": (start + dt.timedelta(days=rng.randint(0, 100), hours=rng.randint(0, 23))).isoformat(),
            "author_username": rng.choice(_DEMO_AUTHORS),
            "platform": rng.choice(_DEMO_PLATFORMS),
            "language": lang,
            "text": text,
            "like_count": max(0, int(rng.gauss(15, 25))),
            "share_count": max(0, int(rng.gauss(5, 10))),
            "view_count": max(0, int(rng.gauss(300, 400))),
        })
    return pd.DataFrame(rows)


def demo_dataframe_csv() -> bytes:
    df = generate_demo_dataframe()
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


# ─── Recipes ────────────────────────────────────────────────────────────────
RECIPES = [
    {
        "id": "first_corpus",
        "title": "Your first corpus in 5 minutes",
        "what": "Upload a CSV, build a corpus, and check the quality.",
        "steps": [
            {"n": 1, "page": "import", "text": "Import → Upload a CSV. Use the demo CSV if you don't have one yet."},
            {"n": 2, "page": "import", "text": "Review the auto-detected column mapping. Fix any reds, hit Save."},
            {"n": 3, "page": "corpus", "text": "Corpus → Build corpus. Leave dedupe on."},
            {"n": 4, "page": "corpus", "text": "Skim the first few rows and any quality flags."},
        ],
    },
    {
        "id": "sample_code",
        "title": "Code a sample, not the whole haystack",
        "what": "Draw a representative sample, then tag it by hand or by AI.",
        "steps": [
            {"n": 1, "page": "slicer", "text": "Slicer → Draw a 200-row stratified sample by platform."},
            {"n": 2, "page": "coding", "text": "Create a codebook with 3–5 categories and shortcut keys."},
            {"n": 3, "page": "coding", "text": "Tag the sample by hand with keyboard shortcuts."},
            {"n": 4, "page": "coding", "text": "Run AI on the same sample and compare — Cohen's κ shows agreement."},
        ],
    },
    {
        "id": "topics_report",
        "title": "From topics to a draft report",
        "what": "Induce topics on a sample, classify the corpus, and export a report.",
        "steps": [
            {"n": 1, "page": "topics", "text": "Topics → Induce on a 500-row sample (cost ≈ $0.20)."},
            {"n": 2, "page": "topics", "text": "Name the topics; merge near-duplicates."},
            {"n": 3, "page": "topics", "text": "Classify the full corpus. Watch the budget envelope."},
            {"n": 4, "page": "analytics", "text": "Check topic trends over time + engagement per topic."},
            {"n": 5, "page": "report", "text": "Generate a Markdown report — paste it into your draft."},
        ],
    },
    {
        "id": "check_drift",
        "title": "Check whether your sample is representative",
        "what": "Compare your sample's distribution against the full corpus before relying on it.",
        "steps": [
            {"n": 1, "page": "slicer", "text": "Draw or open a sample."},
            {"n": 2, "page": "coding", "text": "Run 'Diagnose sample' — see platform, language, date drift."},
            {"n": 3, "page": "coding", "text": "If any distance > 0.15, re-sample with stratification."},
        ],
    },
]


def list_recipes() -> List[Dict[str, Any]]:
    return RECIPES


def get_recipe(recipe_id: str) -> Dict[str, Any]:
    for r in RECIPES:
        if r["id"] == recipe_id:
            return r
    return {}


# ─── Contextual help ────────────────────────────────────────────────────────
HELP: Dict[str, Dict[str, str]] = {
    "corpus": {
        "title": "Working corpus",
        "body": (
            "A corpus is the combined, de-duplicated view across your uploaded datasets. "
            "Each rebuild creates a new snapshot — you can switch back to an earlier one at any time."
        ),
    },
    "coding": {
        "title": "Coding",
        "body": (
            "Codes (aka tags or categories) are how you attach meaning to posts. "
            "Start with 3–5 categories, define exclusion groups to prevent conflicts, "
            "and use shortcut keys for fast manual coding."
        ),
    },
    "topics": {
        "title": "Topic induction",
        "body": (
            "Topics are discovered automatically by asking Claude Sonnet to cluster a "
            "representative sample. Always inspect and rename topics before you classify "
            "the full corpus — the quality downstream depends on this."
        ),
    },
    "analytics": {
        "title": "Analytics",
        "body": (
            "All analytics are pure Python (no API calls) and run over the active snapshot. "
            "Crosstabs, trends over time, and engagement-per-category are the most used."
        ),
    },
    "budget": {
        "title": "Cost ceiling",
        "body": (
            "The monthly budget envelope warns at 80 % and blocks at 100 %. "
            "You can override by typing the exact remaining amount — a deliberate friction "
            "so you don't blow through your research budget by accident."
        ),
    },
    "snapshots": {
        "title": "Snapshots",
        "body": (
            "Each time you rebuild the corpus, we keep the previous version as a snapshot. "
            "Activate any one at any time; your analyses run against whichever is active."
        ),
    },
    "spans": {
        "title": "Span annotations",
        "body": (
            "Span-level tags attach a category to a character range in a post, not the whole post. "
            "Useful for detailed discourse analysis."
        ),
    },
    "enrichment": {
        "title": "Enrichment",
        "body": (
            "Sentiment, entity, and stance labels are produced by Claude Haiku and cached by "
            "row hash. Re-running enrichment is free once the cache is warm."
        ),
    },
}


def help_for(topic: str) -> Dict[str, str]:
    entry = HELP.get(topic)
    if not entry:
        return {"title": "", "body": "", "not_found": True, "topic": topic}
    return dict(entry)
