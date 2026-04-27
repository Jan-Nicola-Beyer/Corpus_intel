"""Multimodal-lite (P12) — enrich text with cheap signals from adjacent metadata.

No image/audio parsing. Instead, we extract "multimodal-adjacent" features that
are usually present in exports but rarely used in the text:
- Emoji density + top emoji
- Hashtag density + top hashtags
- URL domain distribution
- Mention distribution
- Text / media split (if a media_url column exists)

These give the researcher a fuller picture of each post without any extra cost.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List

import pandas as pd

# Rough "emoji" detection — any char whose codepoint > 0x1F300 is close enough.
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]")
_HASHTAG_RE = re.compile(r"#(\w+)")
_URL_RE = re.compile(r"https?://([^\s/$.?#].[^\s]*)", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^(?:www\.)?([^/]+)")


def summarise_post(text: str) -> Dict[str, Any]:
    t = str(text or "")
    emojis = _EMOJI_RE.findall(t)
    hashtags = [h.lower() for h in _HASHTAG_RE.findall(t)]
    urls = _URL_RE.findall(t)
    domains = []
    for u in urls:
        m = _DOMAIN_RE.match(u)
        if m:
            domains.append(m.group(1).lower())
    return {
        "char_count": len(t),
        "word_count": len(t.split()),
        "emoji_count": len(emojis),
        "emoji_unique": len(set(emojis)),
        "hashtag_count": len(hashtags),
        "hashtags": hashtags[:20],
        "url_count": len(urls),
        "domains": domains[:10],
    }


def summarise_corpus(df: pd.DataFrame, *, text_col: str = "text",
                     media_col: str = "media_url") -> Dict[str, Any]:
    if df is None or df.empty or text_col not in df.columns:
        return {"n_rows": 0}
    per = df[text_col].fillna("").astype(str).map(summarise_post)
    emoji_counter: Counter = Counter()
    hashtag_counter: Counter = Counter()
    domain_counter: Counter = Counter()
    with_media = 0
    char_lens: List[int] = []
    for _, s in per.items():
        char_lens.append(s["char_count"])
        emoji_counter.update(_EMOJI_RE.findall(""))  # noop line to keep symmetry
        hashtag_counter.update(s["hashtags"])
        domain_counter.update(s["domains"])
    # Real emoji counter pass
    for t in df[text_col].fillna(""):
        emoji_counter.update(_EMOJI_RE.findall(str(t)))
    if media_col in df.columns:
        with_media = int(df[media_col].notna().sum())
    return {
        "n_rows": int(len(df)),
        "with_media": with_media,
        "text_only": int(len(df) - with_media),
        "char_len": {
            "mean": float(pd.Series(char_lens).mean() or 0.0),
            "median": float(pd.Series(char_lens).median() or 0.0),
            "p90": float(pd.Series(char_lens).quantile(0.9) or 0.0),
        },
        "top_emojis": emoji_counter.most_common(20),
        "top_hashtags": hashtag_counter.most_common(20),
        "top_domains": domain_counter.most_common(20),
    }
