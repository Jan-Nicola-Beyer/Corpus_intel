"""TikTok Research API export."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "tiktok"
SOURCE_NAME = "TikTok Research API"

CANONICAL_COLUMNS = {
    "video_id":          "post_id",
    "id":                "post_id",
    "create_time":       "created_at",
    "video_description": "text",
    "caption":           "text",
    "username":          "author_handle",
    "display_name":      "author_name",
    "author_id":         "author_id",
    "like_count":        "like_count",
    "comment_count":     "comment_count",
    "share_count":       "share_count",
    "view_count":        "view_count",
    "play_count":        "view_count",
    "region_code":       "country",
    "hashtag_names":     "hashtags",
    "voice_to_text":     "text",
    "video_url":         "url",
}

FINGERPRINT = ["video_id", "video_description", "play_count", "region_code"]
FILENAME_TOKENS = ["tiktok", "tt_"]


def detect(df: pd.DataFrame, filename: str) -> float:
    score = column_match_score(df.columns, FINGERPRINT) * 0.95
    score += filename_hint(filename, FILENAME_TOKENS)
    col_set = {c.lower() for c in df.columns}
    if "video_id" in col_set and ("video_description" in col_set or "caption" in col_set):
        score = max(score, 0.9)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("tiktok")
    out["source_type"] = out["source_type"].fillna("video")
    return out
