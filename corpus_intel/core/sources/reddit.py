"""Reddit exports (Pushshift, PRAW dumps)."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "reddit"
SOURCE_NAME = "Reddit"

CANONICAL_COLUMNS = {
    "id":             "post_id",
    "name":           "post_id",
    "title":          "text",
    "selftext":       "text",
    "body":           "text",
    "author":         "author_handle",
    "subreddit":      "platform",
    "created_utc":    "created_at",
    "score":          "like_count",
    "ups":            "like_count",
    "num_comments":   "comment_count",
    "permalink":      "url",
    "url":            "url",
    "parent_id":      "parent_id",
    "link_id":        "in_reply_to",
}

FINGERPRINT_SUBMISSION = ["id", "title", "selftext", "subreddit", "created_utc", "num_comments"]
FINGERPRINT_COMMENT    = ["id", "body", "subreddit", "parent_id", "link_id", "created_utc"]
FILENAME_TOKENS = ["reddit", "pushshift", "praw"]


def detect(df: pd.DataFrame, filename: str) -> float:
    best = max(
        column_match_score(df.columns, FINGERPRINT_SUBMISSION),
        column_match_score(df.columns, FINGERPRINT_COMMENT),
    )
    score = best * 0.95 + filename_hint(filename, FILENAME_TOKENS)
    cols = {c.lower() for c in df.columns}
    if "subreddit" in cols and ("selftext" in cols or "link_id" in cols):
        score = max(score, 0.9)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    # "platform" got populated from `subreddit` — keep it but also stamp the network
    if out["source_type"].isna().all():
        has_body = any(c.lower() == "body" for c in df.columns)
        out["source_type"] = "comment" if has_body else "post"
    return out
