"""YouTube Data API dumps (video metadata + comments)."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "youtube"
SOURCE_NAME = "YouTube"

CANONICAL_COLUMNS = {
    "videoId":            "post_id",
    "video_id":           "post_id",
    "commentId":          "post_id",
    "comment_id":         "post_id",
    "title":              "text",
    "description":        "text",
    "text":               "text",
    "publishedAt":        "created_at",
    "published_at":       "created_at",
    "channelTitle":       "author_name",
    "channel_title":      "author_name",
    "channelId":          "author_id",
    "authorDisplayName":  "author_handle",
    "author_name":        "author_name",
    "viewCount":          "view_count",
    "view_count":         "view_count",
    "likeCount":          "like_count",
    "like_count":         "like_count",
    "commentCount":       "comment_count",
    "comment_count":      "comment_count",
    "parentId":           "parent_id",
    "videoLink":          "url",
    "link":               "url",
}

FINGERPRINT_VIDEO   = ["videoId", "title", "publishedAt", "channelTitle", "viewCount"]
FINGERPRINT_COMMENT = ["commentId", "videoId", "text", "authorDisplayName", "publishedAt"]
FILENAME_TOKENS = ["youtube", "yt_", "ytdlp"]


def detect(df: pd.DataFrame, filename: str) -> float:
    best = max(
        column_match_score(df.columns, FINGERPRINT_VIDEO),
        column_match_score(df.columns, FINGERPRINT_COMMENT),
    )
    score = best * 0.95 + filename_hint(filename, FILENAME_TOKENS)
    cols = {c.lower() for c in df.columns}
    if "videoid" in cols and "channeltitle" in cols:
        score = max(score, 0.9)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("youtube")
    if out["source_type"].isna().all():
        # Infer from columns: presence of parentId suggests comments
        has_parent = any("parent" in c.lower() for c in df.columns)
        out["source_type"] = "comment" if has_parent else "video"
    return out
