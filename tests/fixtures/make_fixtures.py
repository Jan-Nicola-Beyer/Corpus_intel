"""Generate tiny synthetic fixtures for testing source detection.

Run: python tests/fixtures/make_fixtures.py
Produces CSVs that mimic the column signatures of each supported source.
"""
from __future__ import annotations

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def write_csv(name, rows, dialect_delim=","):
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=dialect_delim)
        w.writerow(rows[0])
        for r in rows[1:]:
            w.writerow(r)
    print(f"wrote {path}")


def write_json(name, payload):
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"wrote {path}")


# ─ Brandwatch ───────────────────────────────────────────────────────────────
write_csv("test_brandwatch.csv", [
    ["Query Name", "Date", "Full Text", "Sentiment", "Author", "Language", "Country", "URL", "Twitter Retweets", "Twitter Likes", "Page Type"],
    ["TestQuery", "2025-01-15T12:00:00", "This is the full text of a post", "positive", "@someone", "en", "DE", "https://example.com/1", 3, 10, "twitter"],
    ["TestQuery", "2025-01-16T09:30:00", "Another example post with words", "neutral", "@user2", "en", "US", "https://example.com/2", 0, 5, "twitter"],
    ["TestQuery", "2025-01-17T14:15:00", "Third post discussing topic X", "negative", "@user3", "de", "DE", "https://example.com/3", 12, 42, "twitter"],
])

# ─ Twitter (snscrape) ───────────────────────────────────────────────────────
write_csv("test_twitter.csv", [
    ["url", "date", "content", "renderedContent", "id", "user.username", "user.displayname", "replyCount", "retweetCount", "likeCount", "lang"],
    ["https://x.com/u/1", "2025-02-01", "Hello tweet one", "Hello tweet one", "111", "alice", "Alice", 0, 2, 12, "en"],
    ["https://x.com/u/2", "2025-02-02", "Hello tweet two", "Hello tweet two", "112", "bob",   "Bob",   3, 5, 45, "en"],
])

# ─ Meta (CrowdTangle) ───────────────────────────────────────────────────────
write_csv("test_meta.csv", [
    ["Page Name", "User Name", "Facebook Id", "Page Category", "Post Created", "Type", "Total Interactions", "Likes", "Comments", "Shares", "Message", "Link", "URL"],
    ["Example Page", "exampleuser", "12345", "Media",  "2025-03-01 10:00", "Status", 400, 200, 40, 160, "Facebook message here", "https://fb.com/post/1", "https://fb.com/post/1"],
    ["Example Page", "exampleuser", "12345", "Media",  "2025-03-02 11:00", "Link",   120, 80,  20, 20,  "Second Facebook post",   "https://fb.com/post/2", "https://fb.com/post/2"],
])

# ─ TikTok ───────────────────────────────────────────────────────────────────
write_csv("test_tiktok.csv", [
    ["video_id", "create_time", "region_code", "video_description", "username", "like_count", "comment_count", "share_count", "view_count", "play_count", "hashtag_names"],
    ["vid_1", "1711977600", "US", "A tiktok clip about X", "tt_user", 42, 3, 2, 1000, 1000, "fun,clip"],
    ["vid_2", "1712064000", "DE", "Another tiktok video",  "tt_user2", 17, 1, 0, 450,  450,  "demo"],
])

# ─ YouTube (video metadata) ─────────────────────────────────────────────────
write_csv("test_youtube.csv", [
    ["videoId", "title", "description", "publishedAt", "channelTitle", "channelId", "viewCount", "likeCount", "commentCount"],
    ["yt1", "Video one", "Description one",  "2025-04-01T00:00:00Z", "ChanA", "c_1", 10000, 120, 30],
    ["yt2", "Video two", "Another desc text","2025-04-02T00:00:00Z", "ChanA", "c_1", 7000,  80,  22],
])

# ─ Reddit (submissions) ─────────────────────────────────────────────────────
write_csv("test_reddit.csv", [
    ["id", "title", "selftext", "subreddit", "author", "created_utc", "score", "num_comments", "permalink"],
    ["t3_a", "Reddit post one", "Self-text body", "datasets", "u_a", 1714608000, 42, 12, "/r/datasets/comments/a"],
    ["t3_b", "Reddit post two", "Another body",   "datasets", "u_b", 1714694400, 17, 3,  "/r/datasets/comments/b"],
])

# ─ Mastodon ─────────────────────────────────────────────────────────────────
write_csv("test_mastodon.csv", [
    ["id", "created_at", "content", "account.username", "account.acct", "favourites_count", "reblogs_count", "replies_count", "language", "url"],
    ["m1", "2025-02-10T10:00:00Z", "Mastodon toot one", "mstdn_user", "mstdn_user@mastodon.social", 5, 1, 0, "en", "https://mastodon.social/@u/1"],
    ["m2", "2025-02-11T11:00:00Z", "Mastodon toot two", "mstdn_user", "mstdn_user@mastodon.social", 2, 0, 1, "en", "https://mastodon.social/@u/2"],
])

# ─ News (GDELT-ish) ─────────────────────────────────────────────────────────
write_csv("test_news.csv", [
    ["url", "title", "seendate", "domain", "sourcecountry", "language"],
    ["https://news.example.com/a", "Breaking news headline A", "20250501T120000Z", "news.example.com", "US", "en"],
    ["https://news.example.com/b", "Another headline B",       "20250502T090000Z", "news.example.com", "DE", "de"],
])

# ─ Generic CSV (unknown shape) ──────────────────────────────────────────────
write_csv("test_generic.csv", [
    ["My Custom ID", "Posted On", "Body", "Who Said It", "Stars"],
    ["row-1", "2025-06-01", "Custom body one", "xander", 4],
    ["row-2", "2025-06-02", "Custom body two", "yana",   2],
])

print("all fixtures written")
