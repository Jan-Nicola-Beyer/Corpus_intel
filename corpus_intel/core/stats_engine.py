"""Statistics engine.

Two kinds of routines live here:

1. **Inter-coder reliability** (Phase 4) — Cohen's κ and Krippendorff's α, both
   in their paired / binary-per-category forms. Pure-python, no pandas.

2. **Corpus analytics** (Phase 7) — descriptives, cross-tabs, time-series and
   n-gram top-terms, all pure-pandas. These power the Analytics tab; they never
   call an LLM. A slice is pre-applied by the endpoint so these functions
   operate on whatever DataFrame subset they get.

Two flavours of IRR are implemented:

* **Cohen's κ** — exactly 2 coders, binary outcome per category
  (did the coder place *this* tag on *this* row?). Ported from V2 datalens
  `ui/frames/coding.py` (see Build notes 2026-04-17).
    Po = observed agreement
    Pe = (p1_yes · p2_yes) + (p1_no · p2_no)
    κ  = (Po − Pe) / (1 − Pe)

* **Krippendorff's α** (nominal) — 2+ coders, nominal labels. Uses the standard
  units-and-values observed/expected disagreement formulation.
    Dₒ = Σ_u Σ_{c₁,c₂} n_uc₁ · (n_uc₂ − δ) / (n_u − 1)
    Dₑ = Σ_{c₁,c₂} n_c₁ · n_c₂_minus_one / (N − 1)     (pair-free)
    α  = 1 − Dₒ / Dₑ
  where δ = 1 for c₁==c₂ else 0. Rows with fewer than 2 coders are ignored.

Notes:
    * IRR routines return `None` for edge cases (zero variation → agreement
      is vacuously perfect; not enough overlap → undefined).
    * Analytics routines return plain dicts/lists so FastAPI can JSON-dump them
      directly.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


# ─── Cohen's κ (per-category, binary) ────────────────────────────────────────
def cohens_kappa_binary(coder_a: List[bool], coder_b: List[bool]) -> Optional[Dict[str, float]]:
    """Binary κ across paired observations (True/False from each coder).

    Returns {kappa, po, pe, n, a_yes, b_yes} or None if n == 0.
    If agreement is trivially total (pe == 1), returns kappa = 1.0.
    """
    if len(coder_a) != len(coder_b):
        raise ValueError("coder_a and coder_b must be the same length.")
    n = len(coder_a)
    if n == 0:
        return None
    agree = sum(1 for x, y in zip(coder_a, coder_b) if x == y)
    a_yes = sum(1 for x in coder_a if x)
    b_yes = sum(1 for x in coder_b if x)
    po = agree / n
    pe_yes = (a_yes / n) * (b_yes / n)
    pe_no = ((n - a_yes) / n) * ((n - b_yes) / n)
    pe = pe_yes + pe_no
    kappa = 1.0 if (1 - pe) == 0 else (po - pe) / (1 - pe)
    return {
        "kappa": float(kappa),
        "po": float(po),
        "pe": float(pe),
        "n": int(n),
        "a_yes": int(a_yes),
        "b_yes": int(b_yes),
    }


def cohens_kappa_per_category(
    rows: List[int],
    tags_by_row: Dict[int, List[Dict[str, Any]]],
    coder_a: str,
    coder_b: str,
    categories: List[str],
    codebook_id: str = "",
) -> Dict[str, Any]:
    """For each category, compute binary κ on the rows where *both* coders
    have at least one tag from the given codebook. Returns per-cat stats + a
    weighted average kappa."""
    # Restrict to rows both coders touched (for that codebook).
    a_rows = _rows_touched_by(tags_by_row, coder_a, codebook_id)
    b_rows = _rows_touched_by(tags_by_row, coder_b, codebook_id)
    overlap = [r for r in rows if r in a_rows and r in b_rows]

    per_cat: Dict[str, Dict[str, float]] = {}
    kappas: List[float] = []
    weights: List[int] = []
    for cat in categories:
        a = [_coder_has(tags_by_row.get(r, []), coder_a, cat, codebook_id) for r in overlap]
        b = [_coder_has(tags_by_row.get(r, []), coder_b, cat, codebook_id) for r in overlap]
        res = cohens_kappa_binary(a, b)
        if res is None:
            continue
        per_cat[cat] = res
        kappas.append(res["kappa"])
        # Weight = rows where *either* coder said yes (keeps rare cats from dominating).
        weights.append(max(1, sum(1 for x, y in zip(a, b) if x or y)))

    weighted = None
    if kappas:
        total_w = sum(weights) or 1
        weighted = sum(k * w for k, w in zip(kappas, weights)) / total_w

    return {
        "method": "cohens_kappa",
        "overlap_rows": len(overlap),
        "categories": per_cat,
        "weighted_kappa": weighted,
        "coder_a": coder_a,
        "coder_b": coder_b,
    }


# ─── Krippendorff's α (nominal, n coders) ────────────────────────────────────
def krippendorffs_alpha_nominal(
    rows: List[int],
    tags_by_row: Dict[int, List[Dict[str, Any]]],
    coders: List[str],
    cat_id: str,
    codebook_id: str = "",
) -> Optional[Dict[str, float]]:
    """Nominal α for one category across an arbitrary number of coders.

    Each row that at least 2 of the listed coders touched contributes as a
    'unit'; each coder's answer for that row/category is 1 if they placed the
    tag, 0 otherwise. Non-participants for that unit are skipped (missing data
    is allowed — that's the point of α).

    Returns {alpha, Do, De, units, observations} or None if < 2 observations.
    """
    # Build per-row coder→value lookup, restricted to coders on the list.
    units: List[List[int]] = []   # each unit = list of 0/1 values (2+ coders)
    for r in rows:
        entries = [e for e in tags_by_row.get(r, [])
                   if (not codebook_id or e.get("codebook_id") == codebook_id)
                   and e.get("coder") in coders]
        if not entries:
            continue
        per_coder_touched: Dict[str, bool] = {}
        per_coder_has_cat: Dict[str, bool] = {}
        for e in entries:
            c = e.get("coder")
            per_coder_touched[c] = True
            if e.get("cat_id") == cat_id:
                per_coder_has_cat[c] = True
        touched_coders = [c for c in coders if per_coder_touched.get(c)]
        if len(touched_coders) < 2:
            continue
        values = [1 if per_coder_has_cat.get(c) else 0 for c in touched_coders]
        units.append(values)

    # Count observations
    total_obs = sum(len(u) for u in units)
    if total_obs < 2:
        return None

    # Observed disagreement (nominal: 0 if equal else 1)
    Do_num = 0.0
    for u in units:
        m = len(u)
        if m < 2:
            continue
        n_yes = sum(u)
        n_no = m - n_yes
        # pairs of unequal values = n_yes * n_no * 2  (for ordered pairs)
        unequal = 2 * n_yes * n_no
        Do_num += unequal / (m - 1)
    Do = Do_num / total_obs

    # Expected disagreement: from marginal value counts.
    total_yes = sum(sum(u) for u in units)
    total_no = total_obs - total_yes
    if total_obs < 2 or (total_yes == 0 or total_no == 0):
        # No variation → perfect agreement by convention.
        return {"alpha": 1.0, "Do": 0.0, "De": 0.0, "units": len(units), "observations": total_obs}
    De_num = 2 * total_yes * total_no
    De = De_num / (total_obs * (total_obs - 1))

    alpha = 1.0 - (Do / De) if De > 0 else 1.0
    return {
        "alpha": float(alpha),
        "Do": float(Do),
        "De": float(De),
        "units": int(len(units)),
        "observations": int(total_obs),
    }


def krippendorffs_alpha_per_category(
    rows: List[int],
    tags_by_row: Dict[int, List[Dict[str, Any]]],
    coders: List[str],
    categories: List[str],
    codebook_id: str = "",
) -> Dict[str, Any]:
    per_cat: Dict[str, Optional[Dict[str, float]]] = {}
    alphas: List[float] = []
    for cat in categories:
        res = krippendorffs_alpha_nominal(rows, tags_by_row, coders, cat, codebook_id)
        per_cat[cat] = res
        if res and res.get("alpha") is not None:
            alphas.append(res["alpha"])
    mean_alpha = (sum(alphas) / len(alphas)) if alphas else None
    return {
        "method": "krippendorff_alpha",
        "coders": coders,
        "categories": per_cat,
        "mean_alpha": mean_alpha,
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _coder_has(entries: List[Dict[str, Any]], coder: str, cat_id: str, codebook_id: str) -> bool:
    for e in entries:
        if e.get("coder") == coder and e.get("cat_id") == cat_id:
            if not codebook_id or e.get("codebook_id") == codebook_id:
                return True
    return False


def _rows_touched_by(
    tags_by_row: Dict[int, List[Dict[str, Any]]],
    coder: str,
    codebook_id: str,
) -> set:
    out = set()
    for r, entries in tags_by_row.items():
        for e in entries:
            if e.get("coder") == coder and (not codebook_id or e.get("codebook_id") == codebook_id):
                out.add(r)
                break
    return out


def coder_overlap(
    tags_by_row: Dict[int, List[Dict[str, Any]]],
    codebook_id: str = "",
) -> Dict[str, Any]:
    """Summarise which coders have touched how many rows and how much overlap
    exists between each pair. Used by the UI to decide whether IRR is meaningful."""
    coders: Dict[str, set] = {}
    for r, entries in tags_by_row.items():
        for e in entries:
            if codebook_id and e.get("codebook_id") != codebook_id:
                continue
            c = e.get("coder") or ""
            if not c:
                continue
            coders.setdefault(c, set()).add(int(r) if str(r).isdigit() else r)

    per_coder = {c: len(rows) for c, rows in coders.items()}
    names = sorted(coders)
    pairs: List[Dict[str, Any]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = coders[a] & coders[b]
            pairs.append({"a": a, "b": b, "overlap": len(both)})
    return {
        "coders": per_coder,
        "pairs": pairs,
    }


# ─── Corpus analytics (Phase 7) ──────────────────────────────────────────────
# Columns that never make sense to slice or group by (identifiers, long text).
_NON_CATEGORICAL = {
    "_row_idx", "text", "url", "post_id", "author_id", "in_reply_to", "parent_id",
}

# Low-cardinality columns the UI should offer as row/col dimensions by default.
_DEFAULT_CATEGORICAL_CANDIDATES = [
    "tag", "topic",
    "platform", "source_type", "language", "country", "region",
    "source_dataset", "source_id", "sentiment_source",
]

# Engagement-style numeric columns whose means are worth showing as tiles.
_ENGAGEMENT_COLS = ["like_count", "share_count", "comment_count", "view_count"]

# Frequencies accepted by timeseries(). Pandas offset aliases.
_FREQ_ALIASES = {
    "hour": "H", "H": "H",
    "day":  "D", "D": "D",
    "week": "W", "W": "W",
    "month": "MS", "M": "MS", "MS": "MS",
    "quarter": "QS", "Q": "QS", "QS": "QS",
    "year": "YS", "Y": "YS", "YS": "YS",
}

# Per-language stopword lists. Kept small and explicit — we don't ship NLTK.
# The UI exposes a dropdown so analysts on non-English corpora (German, French,
# Italian, etc.) aren't stuck with English-only filtering.
_STOPWORDS_BY_LANG: Dict[str, set] = {
    "en": {
        "the", "a", "an", "and", "or", "but", "if", "then", "so", "of", "to", "in", "on",
        "for", "with", "by", "at", "as", "is", "are", "was", "were", "be", "been", "being",
        "am", "do", "does", "did", "doing", "have", "has", "had", "having",
        "i", "me", "my", "we", "us", "our", "you", "your", "he", "she", "it", "they",
        "them", "their", "this", "that", "these", "those", "there", "here", "what",
        "which", "who", "whom", "how", "when", "where", "why", "not", "no", "yes",
        "from", "up", "down", "out", "about", "over", "after", "before", "just",
        "than", "too", "very", "can", "will", "would", "could", "should", "may",
        "might", "must", "all", "any", "some", "one", "two", "more", "most", "other",
        "only", "like", "also", "its", "it's", "im", "i'm", "don't", "dont",
    },
    "es": {
        "de", "la", "el", "y", "que", "en", "un", "una", "los", "las", "por", "con",
        "para", "es", "se", "del", "al", "lo", "como", "pero", "más", "está", "están",
        "son", "ser", "fue", "han", "hay", "este", "esta", "eso", "ese", "esa",
        "yo", "tú", "él", "ella", "nosotros", "vosotros", "ellos", "ellas", "mi", "tu",
        "su", "sus", "si", "no", "sí", "ya", "muy", "también", "porque", "todo", "todos",
    },
    "pt": {
        "de", "a", "o", "e", "que", "do", "da", "em", "um", "uma", "para", "com",
        "não", "os", "as", "por", "mais", "mas", "como", "foi", "são", "está", "estão",
        "pela", "pelo", "pelos", "pelas", "eu", "tu", "ele", "ela", "nós", "vós", "eles", "elas",
        "se", "isso", "isto", "esse", "essa", "aquele", "aquela", "já", "muito", "também",
    },
    "de": {
        "der", "die", "das", "und", "oder", "aber", "nicht", "nein", "ja", "ein", "eine", "einen",
        "einer", "eines", "ist", "sind", "war", "waren", "sein", "wird", "werden", "wurde",
        "ich", "du", "er", "sie", "es", "wir", "ihr", "mich", "dich", "ihn", "uns", "euch",
        "mein", "dein", "sein", "ihr", "unser", "euer", "in", "an", "auf", "mit", "bei",
        "von", "zu", "zur", "zum", "für", "aus", "nach", "über", "unter", "vor", "hinter",
        "dass", "weil", "wenn", "wie", "was", "wer", "wo", "doch", "auch", "noch", "schon",
        "so", "aber", "nur", "selbst", "immer", "sehr", "mehr", "alle", "alles", "viel",
    },
    "fr": {
        "le", "la", "les", "un", "une", "des", "de", "du", "au", "aux", "et", "ou",
        "mais", "donc", "car", "ni", "que", "qui", "quoi", "dont", "où", "ne", "pas",
        "plus", "moins", "aussi", "très", "bien", "trop", "tous", "toute", "toutes",
        "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "me", "te", "se",
        "mon", "ton", "son", "notre", "votre", "leur", "leurs",
        "est", "sont", "était", "étaient", "sera", "être", "avoir", "a", "avait",
        "dans", "sur", "sous", "avec", "sans", "pour", "par", "entre", "chez",
    },
    "it": {
        "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "e", "ed", "o", "ma",
        "di", "da", "in", "con", "su", "per", "tra", "fra", "al", "del", "dello",
        "non", "sì", "no", "più", "meno", "tutti", "tutto", "ogni", "questo", "quello",
        "io", "tu", "lui", "lei", "noi", "voi", "loro", "mi", "ti", "si", "ci", "vi",
        "è", "sono", "era", "erano", "sarà", "essere", "avere", "ha", "aveva",
    },
    "nl": {
        "de", "het", "een", "en", "of", "maar", "niet", "is", "zijn", "was", "waren",
        "ben", "bent", "heb", "hebt", "heeft", "hadden", "had", "worden", "wordt",
        "ik", "jij", "hij", "zij", "wij", "jullie", "u", "mij", "me", "jou", "hem", "haar",
        "ons", "mijn", "jouw", "zijn", "ons", "hun", "deze", "dit", "die", "dat",
        "in", "op", "aan", "met", "voor", "door", "van", "naar", "uit", "bij",
        "ook", "nog", "wel", "zo", "als", "om", "te", "maar", "weer",
    },
}

# URL / platform chrome — always filtered when stopwords are on, regardless of language.
_URL_CHROME = {"http", "https", "www", "com", "rt", "amp", "via", "pic", "twitter"}

# Default set combines EN + ES + PT + URL chrome — matches prior behaviour when no
# language is specified.
_STOPWORDS = (
    _STOPWORDS_BY_LANG["en"]
    | _STOPWORDS_BY_LANG["es"]
    | _STOPWORDS_BY_LANG["pt"]
    | _URL_CHROME
)


def stopwords_for_langs(langs: Optional[Iterable[str]]) -> set:
    """Build a stopword set for the requested language codes. Unknown codes are
    ignored. ``None`` or empty input returns the default (EN+ES+PT+URL chrome)."""
    if not langs:
        return set(_STOPWORDS)
    out: set = set(_URL_CHROME)
    for code in langs:
        key = (code or "").strip().lower()
        bucket = _STOPWORDS_BY_LANG.get(key)
        if bucket:
            out.update(bucket)
    return out


def available_stopword_languages() -> List[Dict[str, str]]:
    """Return the UI-facing list of supported stopword languages."""
    return [
        {"code": "en", "label": "English"},
        {"code": "es", "label": "Spanish"},
        {"code": "pt", "label": "Portuguese"},
        {"code": "de", "label": "German"},
        {"code": "fr", "label": "French"},
        {"code": "it", "label": "Italian"},
        {"code": "nl", "label": "Dutch"},
    ]

_TOKEN_RE = re.compile(r"[#@]?[A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f'’\-]{1,}", re.UNICODE)


def list_analytic_columns(df: pd.DataFrame) -> Dict[str, List[str]]:
    """Return which columns the Analytics UI should expose as categorical,
    numeric, datetime, or list-of-strings dimensions."""
    categorical: List[str] = []
    numeric: List[str] = []
    datetime_cols: List[str] = []
    list_cols: List[str] = []
    for c in df.columns:
        if c in _NON_CATEGORICAL and c not in ("post_id",):
            pass
        if c == "_row_idx":
            continue
        series = df[c]
        if pd.api.types.is_datetime64_any_dtype(series):
            datetime_cols.append(c)
            continue
        if pd.api.types.is_numeric_dtype(series):
            if c != "_row_idx":
                numeric.append(c)
            continue
        if c in _NON_CATEGORICAL:
            continue
        # Detect list-valued columns (hashtags, mentions, media_urls)
        first_non_null = next((v for v in series.head(200) if isinstance(v, (list, tuple))), None)
        if first_non_null is not None:
            list_cols.append(c)
            continue
        # String / object → treat as categorical if cardinality is reasonable.
        try:
            nunique = series.nunique(dropna=True)
        except TypeError:
            continue
        if 1 <= nunique <= max(200, int(len(df) * 0.2)):
            categorical.append(c)
    # Always promote the schema "favourites" if they're present.
    for col in _DEFAULT_CATEGORICAL_CANDIDATES:
        if col in df.columns and col not in categorical:
            categorical.append(col)
    # Stable order: schema defaults first, then the rest alphabetically.
    schema_first = [c for c in _DEFAULT_CATEGORICAL_CANDIDATES if c in categorical]
    extras = sorted([c for c in categorical if c not in schema_first])
    return {
        "categorical": schema_first + extras,
        "numeric": sorted(numeric),
        "datetime": datetime_cols,
        "list": sorted(list_cols),
    }


def _top_counts(series: pd.Series, k: int) -> List[Dict[str, Any]]:
    s = series.dropna()
    if s.empty:
        return []
    counts = s.astype("string").value_counts().head(k)
    total = int(s.shape[0])
    return [
        {"value": str(label), "count": int(n), "share": round(int(n) / total, 4) if total else 0.0}
        for label, n in counts.items()
    ]


def _numeric_summary(series: pd.Series) -> Optional[Dict[str, float]]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return None
    return {
        "count": int(s.shape[0]),
        "mean": float(s.mean()),
        "median": float(s.median()),
        "sum": float(s.sum()),
        "min": float(s.min()),
        "max": float(s.max()),
    }


def _explode_list_col(series: pd.Series) -> pd.Series:
    """Flatten a list-valued column to one value per row. Tolerates mixed types."""
    out: List[str] = []
    for v in series.dropna():
        if isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v if x is not None)
        elif isinstance(v, str) and v.strip():
            # best-effort split on commas / whitespace for pre-exploded string cols
            for piece in re.split(r"[,\s]+", v):
                if piece:
                    out.append(piece)
    return pd.Series(out, dtype="string")


def descriptives(df: pd.DataFrame, top_k: int = 10) -> Dict[str, Any]:
    """High-level snapshot of a corpus or slice. Pure pandas, no AI."""
    rows = int(len(df))
    out: Dict[str, Any] = {"rows": rows}
    if rows == 0:
        return out

    # Date coverage
    if "created_at" in df.columns:
        dt = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
        if dt.notna().any():
            out["date_min"] = dt.min().isoformat()
            out["date_max"] = dt.max().isoformat()
            out["date_known_rows"] = int(dt.notna().sum())
            span_days = (dt.max() - dt.min()).total_seconds() / 86400.0
            out["date_span_days"] = round(span_days, 2)

    # Unique authors (prefer handle, fall back to id then name)
    for col in ("author_handle", "author_id", "author_name"):
        if col in df.columns:
            uniq = int(df[col].dropna().astype("string").str.strip().replace("", pd.NA).dropna().nunique())
            if uniq:
                out.setdefault("unique_authors", uniq)
                out.setdefault("unique_authors_col", col)
                break

    # Top faceted columns
    facets: Dict[str, List[Dict[str, Any]]] = {}
    for col in ("platform", "language", "country", "source_type", "source_dataset", "sentiment_source"):
        if col in df.columns:
            facets[col] = _top_counts(df[col], top_k)
    out["facets"] = facets

    # Top authors
    handle_col = next((c for c in ("author_handle", "author_name", "author_id") if c in df.columns), None)
    if handle_col:
        out["top_authors"] = _top_counts(df[handle_col], top_k)
        out["top_authors_col"] = handle_col

    # Engagement summaries
    engagement: Dict[str, Optional[Dict[str, float]]] = {}
    for col in _ENGAGEMENT_COLS:
        if col in df.columns:
            summary = _numeric_summary(df[col])
            if summary:
                engagement[col] = summary
    if engagement:
        out["engagement"] = engagement

    # Top authors ranked by summed engagement (first available metric).
    # Useful for "who has the loudest megaphone" rather than "who posts most".
    if handle_col and engagement:
        metric_col = next((c for c in _ENGAGEMENT_COLS if c in df.columns), None)
        if metric_col:
            grp = pd.DataFrame({
                "_h": df[handle_col].astype("string").str.strip().replace("", pd.NA),
                "_v": pd.to_numeric(df[metric_col], errors="coerce").fillna(0.0),
            }).dropna(subset=["_h"])
            if not grp.empty:
                ranked = grp.groupby("_h")["_v"].sum().sort_values(ascending=False).head(top_k)
                total = float(ranked.sum()) if ranked.sum() else 0.0
                out["top_authors_by_engagement"] = [
                    {"value": str(h), "sum": round(float(v), 2),
                     "share": round(float(v) / total, 4) if total else 0.0}
                    for h, v in ranked.items() if float(v) > 0
                ]
                out["top_authors_by_engagement_metric"] = metric_col

    # List-valued columns (hashtags, mentions, media_urls)
    list_breakdown: Dict[str, List[Dict[str, Any]]] = {}
    for col in ("hashtags", "mentions", "media_urls"):
        if col in df.columns:
            flat = _explode_list_col(df[col])
            top = _top_counts(flat, top_k)
            if top:
                list_breakdown[col] = top
    if list_breakdown:
        out["lists"] = list_breakdown

    # Text coverage
    if "text" in df.columns:
        texts = df["text"].astype("string")
        non_empty = texts.fillna("").str.strip().str.len() > 0
        out["text_nonempty_rows"] = int(non_empty.sum())
        if non_empty.any():
            lengths = texts[non_empty].str.len()
            out["text_length"] = {
                "mean": float(lengths.mean()),
                "median": float(lengths.median()),
                "p95": float(lengths.quantile(0.95)) if len(lengths) >= 20 else float(lengths.max()),
            }

    return out


def crosstab(
    df: pd.DataFrame,
    row_col: str,
    col_col: str,
    *,
    normalize: Optional[str] = None,
    top_k_rows: int = 25,
    top_k_cols: int = 25,
) -> Dict[str, Any]:
    """2-D cross-tabulation between two categorical columns.

    `normalize` is one of ``None``, ``"row"``, ``"col"``, ``"all"`` — matching the
    pandas convention. Output is a plain dict of labels + 2-D list of values
    (JSON-friendly).
    """
    if row_col not in df.columns:
        raise ValueError(f"row column {row_col!r} not found")
    if col_col not in df.columns:
        raise ValueError(f"column {col_col!r} not found")

    # Use string dtype so NaN groups collapse; drop empty labels.
    row_vals = df[row_col].astype("string").fillna("—")
    col_vals = df[col_col].astype("string").fillna("—")

    # Keep only top categories per axis to bound output size.
    top_rows = row_vals.value_counts().head(top_k_rows).index.tolist()
    top_cols = col_vals.value_counts().head(top_k_cols).index.tolist()
    mask = row_vals.isin(top_rows) & col_vals.isin(top_cols)
    ct = pd.crosstab(row_vals[mask], col_vals[mask])

    # Restore stable order (most-frequent first).
    ct = ct.reindex(index=top_rows, columns=top_cols, fill_value=0)

    counts = ct.to_numpy().tolist()
    totals_row = ct.sum(axis=1).to_numpy().tolist()
    totals_col = ct.sum(axis=0).to_numpy().tolist()
    grand = int(ct.to_numpy().sum())

    normalized: Optional[List[List[float]]] = None
    if normalize in ("row", "col", "all"):
        axis_arg = {"row": "index", "col": "columns", "all": "all"}[normalize]
        norm = pd.crosstab(row_vals[mask], col_vals[mask], normalize=axis_arg)
        norm = norm.reindex(index=top_rows, columns=top_cols, fill_value=0.0)
        normalized = [[round(float(x), 6) for x in row] for row in norm.to_numpy().tolist()]

    return {
        "row_col": row_col,
        "col_col": col_col,
        "row_labels": [str(x) for x in ct.index.tolist()],
        "col_labels": [str(x) for x in ct.columns.tolist()],
        "counts": counts,
        "totals_row": totals_row,
        "totals_col": totals_col,
        "total": grand,
        "rows_in_input": int(len(df)),
        "rows_counted": int(mask.sum()),
        "truncated_rows": bool(row_vals.nunique(dropna=True) > top_k_rows),
        "truncated_cols": bool(col_vals.nunique(dropna=True) > top_k_cols),
        "normalize": normalize,
        "normalized": normalized,
    }


def timeseries(
    df: pd.DataFrame,
    *,
    date_col: str = "created_at",
    freq: str = "day",
    group_col: Optional[str] = None,
    top_k_groups: int = 8,
    agg: str = "count",
    value_col: Optional[str] = None,
) -> Dict[str, Any]:
    """Resample over time. ``agg`` is ``count`` (rows per bucket), ``sum`` or
    ``mean`` (of ``value_col`` — a numeric column like ``like_count``).
    If ``group_col`` is given, returns a stacked series (one line per top-K
    group value plus an "Other" bucket)."""
    if date_col not in df.columns:
        raise ValueError(f"date column {date_col!r} not found")
    agg = (agg or "count").lower()
    if agg not in ("count", "sum", "mean"):
        raise ValueError(f"agg must be count|sum|mean, got {agg!r}")
    if agg in ("sum", "mean"):
        if not value_col:
            raise ValueError(f"agg={agg!r} requires value_col")
        if value_col not in df.columns:
            raise ValueError(f"value column {value_col!r} not found")
    pandas_freq = _FREQ_ALIASES.get(freq, freq)
    dt = pd.to_datetime(df[date_col], errors="coerce", utc=True)
    valid = dt.notna()
    if not valid.any():
        return {"freq": freq, "pandas_freq": pandas_freq, "buckets": [],
                "series": [], "rows_counted": 0, "group_col": group_col,
                "agg": agg, "value_col": value_col}

    work = pd.DataFrame({"_ts": dt[valid]})
    if agg in ("sum", "mean"):
        work["_val"] = pd.to_numeric(df.loc[valid, value_col], errors="coerce").fillna(0.0)
    if group_col and group_col in df.columns:
        work["_group"] = df.loc[valid, group_col].astype("string").fillna("—")
        top_groups = work["_group"].value_counts().head(top_k_groups).index.tolist()
        work["_group"] = work["_group"].where(work["_group"].isin(top_groups), other="Other")
        group_order = top_groups + (["Other"] if (work["_group"] == "Other").any() else [])
    else:
        work["_group"] = "All"
        group_order = ["All"]

    work = work.set_index("_ts")
    gb = work.groupby([pd.Grouper(freq=pandas_freq), "_group"])
    if agg == "count":
        grouped = gb.size().unstack(fill_value=0)
    elif agg == "sum":
        grouped = gb["_val"].sum().unstack(fill_value=0.0)
    else:  # mean
        grouped = gb["_val"].mean().unstack(fill_value=0.0)
    grouped = grouped.reindex(columns=group_order, fill_value=0).sort_index()

    buckets = [ts.isoformat() for ts in grouped.index]
    cast = int if agg == "count" else (lambda x: round(float(x), 4))
    series = [
        {"name": str(col), "data": [cast(x) for x in grouped[col].tolist()]}
        for col in grouped.columns
    ]
    return {
        "freq": freq,
        "pandas_freq": pandas_freq,
        "group_col": group_col,
        "buckets": buckets,
        "series": series,
        "rows_counted": int(valid.sum()),
        "agg": agg,
        "value_col": value_col,
    }


def _tokenize_text(text: str) -> List[str]:
    if not text:
        return []
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


def ngrams(
    df: pd.DataFrame,
    *,
    text_col: str = "text",
    n: int = 1,
    top_k: int = 25,
    min_count: int = 2,
    drop_stopwords: bool = True,
    extra_stopwords: Optional[Iterable[str]] = None,
    stopword_langs: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Top-K n-grams across the text column. Tokenisation is a simple regex
    over word characters (with accents) plus an optional stopword drop. Good
    enough to spotlight signal terms; users who need NLP-grade tokenisation can
    export the corpus.

    ``stopword_langs`` lets callers pick which language(s) of stopwords to drop,
    e.g. ``["de"]`` for German-only filtering. Omitted or empty → EN+ES+PT default.
    """
    if text_col not in df.columns:
        raise ValueError(f"text column {text_col!r} not found")
    n = max(1, int(n))
    top_k = max(1, int(top_k))

    stop = stopwords_for_langs(stopword_langs) if drop_stopwords else set()
    if extra_stopwords:
        stop.update(w.lower().strip() for w in extra_stopwords if w)

    counter: Counter = Counter()
    doc_freq: Counter = Counter()
    docs = 0
    for raw in df[text_col].dropna().astype(str):
        docs += 1
        tokens = [t for t in _tokenize_text(raw) if (len(t) > 1 and (t not in stop))]
        if len(tokens) < n:
            continue
        seen_in_doc: set = set()
        for i in range(len(tokens) - n + 1):
            gram = " ".join(tokens[i:i + n])
            counter[gram] += 1
            if gram not in seen_in_doc:
                doc_freq[gram] += 1
                seen_in_doc.add(gram)

    filtered = [(g, c) for g, c in counter.most_common() if c >= min_count]
    top = filtered[:top_k]
    items = [
        {"gram": g, "count": int(c), "doc_count": int(doc_freq[g]),
         "doc_share": round(int(doc_freq[g]) / docs, 4) if docs else 0.0}
        for g, c in top
    ]
    return {
        "n": n,
        "top_k": top_k,
        "min_count": min_count,
        "docs": docs,
        "vocabulary": int(len(counter)),
        "items": items,
    }


def compare_descriptives(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight diff of two ``descriptives()`` results. Computes absolute
    and relative deltas for row count, unique authors, and engagement means so
    the UI can render a side-by-side comparison card."""
    def _delta(x: Optional[float], y: Optional[float]) -> Dict[str, Any]:
        if x is None or y is None:
            return {"a": x, "b": y, "abs": None, "rel": None}
        abs_d = float(y) - float(x)
        rel = (abs_d / x) if x else None
        return {"a": x, "b": y, "abs": abs_d, "rel": rel}

    out: Dict[str, Any] = {
        "rows":            _delta(a.get("rows"), b.get("rows")),
        "unique_authors":  _delta(a.get("unique_authors"), b.get("unique_authors")),
    }
    # Engagement means by column
    eng_out: Dict[str, Dict[str, Any]] = {}
    a_eng = a.get("engagement") or {}
    b_eng = b.get("engagement") or {}
    for col in set(a_eng) | set(b_eng):
        ea = (a_eng.get(col) or {}).get("mean")
        eb = (b_eng.get(col) or {}).get("mean")
        eng_out[col] = _delta(ea, eb)
    if eng_out:
        out["engagement_mean"] = eng_out
    return out
