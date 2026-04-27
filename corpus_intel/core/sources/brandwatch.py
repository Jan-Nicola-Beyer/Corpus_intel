"""Brandwatch CSV export."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "brandwatch"
SOURCE_NAME = "Brandwatch"

# Source column → canonical column
CANONICAL_COLUMNS = {
    "Resource Id":         "post_id",
    "Resource Type":       "source_type",
    "Date":                "created_at",
    "Full Text":           "text",
    "Sentiment":           "sentiment_source",
    "Author":              "author_handle",
    "Language":            "language",
    "Country":             "country",
    "Region":              "region",
    "URL":                 "url",
    "Twitter Retweets":    "share_count",
    "Twitter Likes":       "like_count",
    "Twitter Reply Count": "comment_count",
    "Impressions":         "view_count",
    "Page Type":           "platform",
    "Hashtags":            "hashtags",
    "Mentioned Authors":   "mentions",
}

FINGERPRINT = ["Query Name", "Full Text", "Sentiment", "Date"]
FILENAME_TOKENS = ["brandwatch", "bw_"]


def detect(df: pd.DataFrame, filename: str) -> float:
    score = column_match_score(df.columns, FINGERPRINT) * 0.9
    score += filename_hint(filename, FILENAME_TOKENS)
    # Presence of "Query Name" + "Full Text" is near-definitive for Brandwatch
    col_set = {c.lower() for c in df.columns}
    if "query name" in col_set and "full text" in col_set:
        score = max(score, 0.95)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("brandwatch")
    return out
