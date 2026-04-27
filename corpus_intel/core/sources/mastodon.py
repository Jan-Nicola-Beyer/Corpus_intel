"""Mastodon / fediverse exports."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "mastodon"
SOURCE_NAME = "Mastodon"

CANONICAL_COLUMNS = {
    "id":                "post_id",
    "uri":               "url",
    "url":               "url",
    "created_at":        "created_at",
    "content":           "text",
    "spoiler_text":      "text",
    "account.username":  "author_handle",
    "account.acct":      "author_handle",
    "account.id":        "author_id",
    "account.display_name": "author_name",
    "favourites_count":  "like_count",
    "reblogs_count":     "share_count",
    "replies_count":     "comment_count",
    "in_reply_to_id":    "in_reply_to",
    "in_reply_to_account_id": "parent_id",
    "language":          "language",
    "visibility":        "source_type",
}

FINGERPRINT = ["favourites_count", "reblogs_count", "replies_count", "account.acct"]
FINGERPRINT_FLAT = ["favourites_count", "reblogs_count", "replies_count", "acct"]
FILENAME_TOKENS = ["mastodon", "mstdn", "fediverse"]


def detect(df: pd.DataFrame, filename: str) -> float:
    best = max(
        column_match_score(df.columns, FINGERPRINT),
        column_match_score(df.columns, FINGERPRINT_FLAT),
    )
    score = best * 0.95 + filename_hint(filename, FILENAME_TOKENS)
    cols = {c.lower() for c in df.columns}
    if "reblogs_count" in cols and "favourites_count" in cols:
        score = max(score, 0.9)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("mastodon")
    return out
