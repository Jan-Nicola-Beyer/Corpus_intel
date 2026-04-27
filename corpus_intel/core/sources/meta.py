"""Meta / Facebook exports — CrowdTangle *and* Meta Content Library (MCL)."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "meta"
SOURCE_NAME = "Meta (CrowdTangle / Content Library)"

# CrowdTangle column headers (Title Case, space-separated) + Content Library
# headers (snake_case, dotted). Both normalise through apply_mapping's
# case-insensitive lookup.
CANONICAL_COLUMNS = {
    # ── CrowdTangle ──
    "Post Url":                  "url",
    "URL":                       "url",
    "Link":                      "url",
    "Facebook Id":               "author_id",
    "Page Name":                 "author_name",
    "User Name":                 "author_handle",
    "Post Created":              "created_at",
    "Post Created Date":         "created_at",
    "Created":                   "created_at",
    "Message":                   "text",
    "Description":               "text",
    "Title":                     "text",
    "Likes":                     "like_count",
    "Comments":                  "comment_count",
    "Shares":                    "share_count",
    "Views":                     "view_count",
    "Total Interactions":        "view_count",
    "Type":                      "source_type",
    "Page Admin Top Country":    "country",
    "Page Category":             "platform",
    # ── Meta Content Library (MCL) ──
    "id":                        "post_id",
    "mcl_url":                   "url",
    "creation_time":             "created_at",
    "lang":                      "language",
    "text":                      "text",
    "hashtags":                  "hashtags",
    "content_type":              "source_type",
    "post_owner.id":             "author_id",
    "post_owner.name":           "author_name",
    "post_owner.username":       "author_handle",
    "statistics.like_count":     "like_count",
    "statistics.comment_count":  "comment_count",
    "statistics.views":          "view_count",
}

# CrowdTangle fingerprint
FINGERPRINT_CT = ["Page Name", "Message", "Post Created", "Total Interactions"]
# Meta Content Library fingerprint — dotted owner/statistics fields are the
# dead giveaway; no other platform export uses this naming.
FINGERPRINT_MCL = [
    "post_owner.username",
    "post_owner.name",
    "statistics.like_count",
    "statistics.comment_count",
    "creation_time",
    "mcl_url",
]
FILENAME_TOKENS = ["crowdtangle", "facebook", "meta_", "ct_", "content_library", "mcl", "download_"]


def detect(df: pd.DataFrame, filename: str) -> float:
    cols_norm = {str(c).strip().lower() for c in df.columns}

    # CrowdTangle branch
    ct_score = column_match_score(df.columns, FINGERPRINT_CT) * 0.95
    if {"page name", "message", "total interactions"}.issubset(cols_norm):
        ct_score = max(ct_score, 0.95)

    # MCL branch — the dotted-namespace columns are unique to Content Library.
    mcl_score = column_match_score(df.columns, FINGERPRINT_MCL) * 0.98
    mcl_signals = {"post_owner.username", "post_owner.name", "statistics.like_count",
                   "statistics.comment_count", "creation_time", "mcl_url", "is_branded_content"}
    hits = sum(1 for c in cols_norm if c in mcl_signals)
    if hits >= 3:
        mcl_score = max(mcl_score, 0.95)
    if hits >= 5:
        mcl_score = max(mcl_score, 0.98)

    score = max(ct_score, mcl_score) + filename_hint(filename, FILENAME_TOKENS)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("facebook")
    return out
