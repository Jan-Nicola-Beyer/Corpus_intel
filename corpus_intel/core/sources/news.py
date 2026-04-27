"""News article exports (GDELT, MediaCloud, RSS aggregators)."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "news"
SOURCE_NAME = "News articles"

CANONICAL_COLUMNS = {
    "url":             "url",
    "link":            "url",
    "title":           "text",
    "seendate":        "created_at",
    "publish_date":    "created_at",
    "publishedAt":     "created_at",
    "date":            "created_at",
    "body":            "text",
    "article_text":    "text",
    "text":            "text",
    "content":         "text",
    "domain":          "author_handle",
    "source":          "author_name",
    "source_country":  "country",
    "sourcecountry":   "country",
    "language":        "language",
    "stories_id":      "post_id",
    "id":              "post_id",
}

FINGERPRINT_GDELT     = ["url", "title", "seendate", "domain", "sourcecountry"]
FINGERPRINT_MEDIACLOUD = ["stories_id", "title", "publish_date", "media_name"]
FINGERPRINT_GENERIC_NEWS = ["url", "title", "publish_date", "language"]
FILENAME_TOKENS = ["gdelt", "mediacloud", "news", "rss", "articles"]


def detect(df: pd.DataFrame, filename: str) -> float:
    best = max(
        column_match_score(df.columns, FINGERPRINT_GDELT),
        column_match_score(df.columns, FINGERPRINT_MEDIACLOUD),
        column_match_score(df.columns, FINGERPRINT_GENERIC_NEWS),
    )
    score = best * 0.85 + filename_hint(filename, FILENAME_TOKENS)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("news")
    out["source_type"] = out["source_type"].fillna("article")
    return out
