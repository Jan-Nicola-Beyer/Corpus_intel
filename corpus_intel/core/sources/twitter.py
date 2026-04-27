"""Twitter / X exports (academic archive, snscrape, tweepy dumps)."""
from __future__ import annotations

import pandas as pd

from ._base import apply_mapping, column_match_score, filename_hint

SOURCE_ID = "twitter"
SOURCE_NAME = "Twitter / X"

CANONICAL_COLUMNS = {
    "id":                         "post_id",
    "tweet_id":                   "post_id",
    "conversation_id":            "parent_id",
    "created_at":                 "created_at",
    "date":                       "created_at",
    "text":                       "text",
    "content":                    "text",
    "renderedContent":            "text",
    "user_screen_name":           "author_handle",
    "user.username":              "author_handle",
    "username":                   "author_handle",
    "screen_name":                "author_handle",
    "user_name":                  "author_name",
    "user.displayname":           "author_name",
    "user.id":                    "author_id",
    "user_id":                    "author_id",
    "retweet_count":              "share_count",
    "retweetCount":               "share_count",
    "favorite_count":             "like_count",
    "like_count":                 "like_count",
    "likeCount":                  "like_count",
    "reply_count":                "comment_count",
    "replyCount":                 "comment_count",
    "in_reply_to_status_id":      "in_reply_to",
    "in_reply_to":                "in_reply_to",
    "lang":                       "language",
    "url":                        "url",
    "hashtags":                   "hashtags",
    "mentioned_users":            "mentions",
}

FINGERPRINT_ACADEMIC = ["id", "text", "created_at", "author_id"]
FINGERPRINT_SNSCRAPE = ["url", "date", "content", "renderedContent", "user"]
FINGERPRINT_TWEEPY   = ["id", "text", "user_screen_name", "retweet_count"]
FILENAME_TOKENS = ["twitter", "tweet", "x_", "snscrape"]


def detect(df: pd.DataFrame, filename: str) -> float:
    best = max(
        column_match_score(df.columns, FINGERPRINT_ACADEMIC),
        column_match_score(df.columns, FINGERPRINT_SNSCRAPE),
        column_match_score(df.columns, FINGERPRINT_TWEEPY),
    )
    score = best * 0.9 + filename_hint(filename, FILENAME_TOKENS)
    # "retweet_count" is a dead giveaway not shared by other platforms
    if any(c.lower() == "retweet_count" or c.lower() == "retweetcount" for c in df.columns):
        score = max(score, 0.9)
    return min(1.0, score)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = apply_mapping(df, CANONICAL_COLUMNS)
    out["platform"] = out["platform"].fillna("twitter")
    out["source_type"] = out["source_type"].fillna("post")
    return out
