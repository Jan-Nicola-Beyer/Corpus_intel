"""Canonical column schema for Corpus Intel.

Every source adapter maps its native columns onto these canonical names.
The fuzzy matcher is used when a source adapter has no pre-wired mapping for a
given column, and as the backbone of the Import UI's mapping table.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CanonicalField:
    name: str
    dtype: str        # "str" | "int" | "float" | "datetime" | "list[str]"
    description: str
    required: bool = False


# Order mirrors §4 of CODING_PLAN.md.
CANONICAL_FIELDS: List[CanonicalField] = [
    CanonicalField("post_id",          "str",      "Stable identifier for the post/row",   required=True),
    CanonicalField("platform",         "str",      "Originating platform (twitter, facebook, ...)"),
    CanonicalField("source_type",      "str",      "post | comment | video | article | reply"),
    CanonicalField("author_id",        "str",      "Platform-level author id"),
    CanonicalField("author_handle",    "str",      "Username / @handle"),
    CanonicalField("author_name",      "str",      "Display name"),
    CanonicalField("text",             "str",      "Primary textual content",                required=True),
    CanonicalField("language",         "str",      "ISO 639-1 language code if known"),
    CanonicalField("created_at",       "datetime", "Publication timestamp (UTC preferred)"),
    CanonicalField("url",              "str",      "Permalink to the post"),
    CanonicalField("like_count",       "int",      "Likes / favourites / upvotes"),
    CanonicalField("share_count",      "int",      "Retweets / shares / reblogs"),
    CanonicalField("comment_count",    "int",      "Replies / comments"),
    CanonicalField("view_count",       "int",      "Impressions / views"),
    CanonicalField("in_reply_to",      "str",      "Id of the post being replied to"),
    CanonicalField("parent_id",        "str",      "Thread parent id"),
    CanonicalField("media_urls",       "list[str]","Attached media URLs"),
    CanonicalField("hashtags",         "list[str]","Parsed hashtags"),
    CanonicalField("mentions",         "list[str]","Parsed @-mentions"),
    CanonicalField("country",          "str",      "ISO country / region"),
    CanonicalField("region",           "str",      "Sub-national region"),
    CanonicalField("sentiment_source", "str",      "Native sentiment label if provided"),
]

CANONICAL_NAMES: List[str] = [f.name for f in CANONICAL_FIELDS]
CANONICAL_BY_NAME: Dict[str, CanonicalField] = {f.name: f for f in CANONICAL_FIELDS}
REQUIRED_CANONICAL: List[str] = [f.name for f in CANONICAL_FIELDS if f.required]


# ─── Fuzzy matching ──────────────────────────────────────────────────────────
# Alias table: canonical_name -> list of common source-column names.
# Used as first-pass before SequenceMatcher fallback.
_ALIASES: Dict[str, List[str]] = {
    "post_id":       ["post id", "id", "tweet id", "video id", "comment id", "message id", "status id", "permalink id"],
    "platform":      ["platform", "network", "channel", "source"],
    "source_type":   ["post type", "content type", "post_type", "content_type"],
    "author_id":     ["user id", "author id", "account id", "channel id", "owner id", "facebook id"],
    "author_handle": ["username", "user name", "screen name", "handle", "account", "acct"],
    "author_name":   ["display name", "author name", "full name", "page name", "channel title"],
    "text":          ["text", "content", "body", "message", "full text", "description",
                      "tweet", "caption", "title", "post", "rendered content", "video description", "selftext"],
    "language":      ["language", "lang", "detected language"],
    "created_at":    ["date", "created at", "created_utc", "created_time", "posted at",
                      "publish date", "published at", "timestamp", "post created", "post created date",
                      "seendate"],
    "url":           ["url", "link", "permalink", "tweet url", "post url"],
    "like_count":    ["likes", "like count", "favorites", "favourites", "favorite count",
                      "favourite count", "upvotes", "score", "reactions", "heart count"],
    "share_count":   ["shares", "share count", "retweets", "retweet count", "reblogs", "reblog count",
                      "repost count"],
    "comment_count": ["comments", "comment count", "replies", "reply count", "num comments"],
    "view_count":    ["views", "view count", "play count", "impressions", "plays"],
    "in_reply_to":   ["in reply to", "in_reply_to_status_id", "in reply to status id", "replied to"],
    "parent_id":     ["parent id", "parent_id", "thread id", "link_id", "conversation id"],
    "media_urls":    ["media urls", "media", "attachments", "image url", "video url"],
    "hashtags":      ["hashtags", "tags", "hashtag names"],
    "mentions":      ["mentions", "mentioned users"],
    "country":       ["country", "country code", "source country", "region code"],
    "region":        ["region", "state", "city", "geo"],
    "sentiment_source": ["sentiment", "native sentiment", "tone"],
}


def _norm(s: str) -> str:
    """Normalize a header for fuzzy matching.

    Unicode-aware: NFKC folds compatibility forms, casefold is the locale-robust
    lowercase, and `ch.isalnum()` in Python 3 accepts any Unicode letter/digit —
    so headers like 'fecha', 'Автор', '日時', 'país' survive. The previous
    `ch.lower()` + ASCII-only filter stripped every non-Latin character."""
    normalized = unicodedata.normalize("NFKC", str(s))
    return "".join(ch.casefold() for ch in normalized if ch.isalnum())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _candidate_forms(col: str) -> List[str]:
    """Expand a source column name into matching candidates.

    Dotted namespaces are common in Content Library / Brandwatch / snscrape
    exports (``post_owner.username``, ``statistics.like_count``, ``user.id``).
    Fuzzy matching against the full dotted form rarely clears threshold, so we
    also try the trailing segment on its own — that's usually the semantic
    field name.

    We only split on dots, never on underscores: a dotted prefix is a
    structural namespace (owner.name = the owner's name), but a snake_case
    phrase like ``is_branded_content`` is a semantic phrase — its tail
    (``content``) is not a field name and would spuriously match the ``text``
    alias. The full form is tried first so specific aliases still win over
    the tail segment.
    """
    forms = [col]
    if "." in col:
        tail = col.rsplit(".", 1)[-1]
        if tail and tail != col:
            forms.append(tail)
    return forms


def suggest_mapping(
    source_columns: List[str],
    pre_wired: Optional[Dict[str, str]] = None,
    threshold: float = 0.85,
) -> Dict[str, Dict[str, object]]:
    """For each source column, propose the best canonical match.

    Returns {source_col: {"canonical": str|None, "confidence": float, "method": str}}.
    `pre_wired` is an adapter-supplied source→canonical mapping that takes priority.
    """
    pre_wired = pre_wired or {}
    pre_wired_norm = {_norm(k): v for k, v in pre_wired.items()}
    suggestions: Dict[str, Dict[str, object]] = {}

    for col in source_columns:
        forms = _candidate_forms(col)
        norm_forms = [_norm(f) for f in forms]

        # 1. Adapter-wired exact match (full form only — tails must not inherit
        #    another column's pre-wired mapping, e.g. post_owner.username
        #    should NOT pick up Twitter's `username → author_handle`).
        hit = pre_wired_norm.get(norm_forms[0])
        if hit:
            suggestions[col] = {"canonical": hit, "confidence": 1.0, "method": "adapter"}
            continue

        # 2. Alias-table exact match (any form)
        matched: Optional[Tuple[str, float]] = None
        for n in norm_forms:
            for canon, aliases in _ALIASES.items():
                if n == _norm(canon) or any(n == _norm(a) for a in aliases):
                    matched = (canon, 0.98)
                    break
            if matched:
                break
        if matched:
            suggestions[col] = {"canonical": matched[0], "confidence": matched[1], "method": "alias"}
            continue

        # 3. Fuzzy SequenceMatcher across canonical + aliases, any form
        best_canon: Optional[str] = None
        best_score = 0.0
        for form in forms:
            for canon, aliases in _ALIASES.items():
                for candidate in [canon, *aliases]:
                    score = _similarity(form, candidate)
                    if score > best_score:
                        best_score = score
                        best_canon = canon
        if best_canon and best_score >= threshold:
            suggestions[col] = {"canonical": best_canon, "confidence": round(best_score, 2), "method": "fuzzy"}
        else:
            suggestions[col] = {"canonical": None, "confidence": round(best_score, 2), "method": "unmatched"}

    return suggestions


def validate_mapping(mapping: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Check a mapping covers required canonical fields and has no dupes."""
    problems: List[str] = []
    targets = list(mapping.values())
    for req in REQUIRED_CANONICAL:
        if req not in targets:
            problems.append(f"missing_required:{req}")
    seen: Dict[str, int] = {}
    for v in targets:
        if not v:
            continue
        seen[v] = seen.get(v, 0) + 1
    for canon, n in seen.items():
        if n > 1:
            problems.append(f"duplicate_target:{canon}")
    return (len(problems) == 0), problems
