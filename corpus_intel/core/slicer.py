"""Brandwatch-style boolean query engine for the corpus text column.

Grammar
-------
    expr     = or_expr
    or_expr  = and_expr  ('OR' and_expr)*
    and_expr = not_expr  ('AND' not_expr | implicit-AND not_expr)*
    not_expr = 'NOT' not_expr | atom
    atom     = '(' expr ')' | PHRASE | WORD

Operators are case-insensitive. Adjacent words without an operator are treated
as an implicit AND. ``"exact phrase"`` does a substring match. ``climat*`` is a
word-boundary wildcard (``climate``, ``climatic``, …).

Ported from ``datalens_v3_opt/ui/frames/slicer.py`` — tkinter was stripped out;
parse errors now raise ``SlicerError`` rather than pop a dialog.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd


class SlicerError(ValueError):
    """Raised when a boolean query cannot be parsed."""


# Token is (kind, value) for simple kinds, or (kind, (field, value, is_phrase))
# for FIELD_TERM. Using Any to avoid the Tuple-of-Tuples mypy headache.
Token = Tuple[str, Any]


# Field alias → (list of corpus columns to OR together, value_matcher_kind)
# value_matcher_kind is one of:
#   "word_or_phrase"  → wildcard-aware word match on phrase, or word-boundary on bare word
#   "substring"       → plain case-insensitive substring (for URLs, ids)
#   "exact_token"     → whole-field equality after lower (for language/country codes)
FIELD_ALIASES: Dict[str, Tuple[List[str], str]] = {
    "author":    (["author_handle", "author_name"], "word_or_phrase"),
    "handle":    (["author_handle"],                "word_or_phrase"),
    "name":      (["author_name"],                  "word_or_phrase"),
    "author_id": (["author_id"],                    "substring"),
    "url":       (["url"],                          "substring"),
    "link":      (["url"],                          "substring"),
    "hashtag":   (["hashtags"],                     "word_or_phrase"),
    "hashtags":  (["hashtags"],                     "word_or_phrase"),
    "tag":       (["hashtags"],                     "word_or_phrase"),
    "language":  (["language"],                     "exact_token"),
    "lang":      (["language"],                     "exact_token"),
    "country":   (["country"],                      "exact_token"),
    "region":    (["region"],                       "exact_token"),
    "platform":  (["platform"],                     "exact_token"),
    "source":    (["source_dataset"],               "substring"),
    "dataset":   (["source_dataset"],               "substring"),
    "id":        (["post_id"],                      "substring"),
    "post_id":   (["post_id"],                      "substring"),
}


_FIELD_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")

# Scripts where characters don't delimit words with whitespace — `\b` doesn't
# fire between two ideographs, so a word-boundary-anchored pattern would never
# match a Chinese / Japanese / Korean / Thai term. Detect any such character in
# a query term and fall back to substring matching.
_NO_WORD_BOUNDARY_RE = re.compile(
    r"[\u3040-\u30FF"      # Hiragana + Katakana
    r"\u3400-\u4DBF"       # CJK Unified Ideographs Extension A
    r"\u4E00-\u9FFF"       # CJK Unified Ideographs
    r"\uF900-\uFAFF"       # CJK Compatibility Ideographs
    r"\uAC00-\uD7AF"       # Hangul syllables
    r"\u0E00-\u0E7F"       # Thai
    r"\u1000-\u109F"       # Myanmar
    r"\u1780-\u17FF"       # Khmer
    r"\u0F00-\u0FFF"       # Tibetan
    r"]"
)


def _word_pattern(word: str) -> str:
    """Build a regex that matches `word` as a whole word where script allows.

    - ASCII / Latin / Cyrillic / Greek / Arabic / Hebrew: `\b…\b` (unicode-aware).
    - CJK / Thai / Khmer / …: substring match (these scripts don't have word
      boundaries \\b can detect).
    - `*` is a wildcard (``climat*`` → ``climate``, ``climatic``).
    """
    has_star = "*" in word
    body = re.escape(word).replace(r"\*", r"\w*") if has_star else re.escape(word)
    if _NO_WORD_BOUNDARY_RE.search(word):
        return body  # substring match — \b would never fire between two ideographs
    return r"\b" + body + r"\b"


def _tokenize(query: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
        elif ch == "(":
            tokens.append(("LPAREN", "("))
            i += 1
        elif ch == ")":
            tokens.append(("RPAREN", ")"))
            i += 1
        elif ch == '"':
            j = query.find('"', i + 1)
            if j == -1:
                # unterminated quote — treat the rest of the string as phrase text
                j = n
            tokens.append(("PHRASE", query[i + 1:j]))
            i = j + 1
        else:
            # Walk the raw word first; then check for field:value prefix.
            j = i
            while j < n and not query[j].isspace() and query[j] not in '()':
                # Allow quotes inside a field token so `author:"Donald Trump"` is one token.
                if query[j] == '"':
                    k = query.find('"', j + 1)
                    j = (n if k == -1 else k + 1)
                    continue
                j += 1
            word = query[i:j]
            upper = word.upper()
            m = _FIELD_PREFIX_RE.match(word)
            if m and m.group(1).lower() in FIELD_ALIASES:
                field = m.group(1).lower()
                raw_val = word[m.end():]
                if raw_val.startswith('"') and raw_val.endswith('"') and len(raw_val) >= 2:
                    tokens.append(("FIELD_TERM", (field, raw_val[1:-1], True)))
                else:
                    tokens.append(("FIELD_TERM", (field, raw_val, False)))
            elif upper == "AND":
                tokens.append(("AND", "AND"))
            elif upper == "OR":
                tokens.append(("OR", "OR"))
            elif upper == "NOT":
                tokens.append(("NOT", "NOT"))
            elif upper == "NEAR":
                tokens.append(("AND", "AND"))  # treat NEAR as AND
            elif word:
                tokens.append(("WORD", word))
            i = j
    return tokens


class _BoolParser:
    """Recursive-descent parser. See module docstring for grammar."""

    def __init__(
        self,
        tokens: List[Token],
        series: pd.Series,
        df: Optional[pd.DataFrame] = None,
    ):
        self.tokens = tokens
        self.pos = 0
        # Pre-casefold the text column once — slicer queries are case-insensitive.
        # casefold() (not lower()) handles Turkish dotted-I, German ß, etc.
        self.series = series.fillna("").astype(str).str.casefold()
        self.df = df  # Optional: needed only when field:term atoms are used.
        self._col_cache: Dict[str, pd.Series] = {}

    def _col_lower(self, col: str) -> pd.Series:
        """Return a casefolded string view of df[col], cached."""
        if col in self._col_cache:
            return self._col_cache[col]
        if self.df is None or col not in self.df.columns:
            s = pd.Series([""] * len(self.series), index=self.series.index)
        else:
            s = self.df[col].fillna("").astype(str).str.casefold()
        self._col_cache[col] = s
        return s

    def _peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _consume(self, expected: str | None = None) -> Token:
        if self.pos >= len(self.tokens):
            raise SlicerError("Unexpected end of query.")
        tok = self.tokens[self.pos]
        if expected and tok[0] != expected:
            raise SlicerError(f"Expected {expected}, got {tok[0]!r}.")
        self.pos += 1
        return tok

    def _true(self) -> pd.Series:
        return pd.Series(True, index=self.series.index)

    def parse(self) -> pd.Series:
        if not self.tokens:
            return self._true()
        result = self._or()
        if self._peek() is not None:
            tok = self._peek()
            raise SlicerError(f"Unexpected token {tok[0]!r} near end of query.")
        return result

    def _or(self) -> pd.Series:
        left = self._and()
        while self._peek() and self._peek()[0] == "OR":
            self._consume("OR")
            left = left | self._and()
        return left

    def _and(self) -> pd.Series:
        left = self._not()
        while True:
            tok = self._peek()
            if tok is None:
                break
            if tok[0] == "AND":
                self._consume("AND")
                left = left & self._not()
            elif tok[0] in ("WORD", "PHRASE", "LPAREN", "NOT", "FIELD_TERM"):
                left = left & self._not()  # implicit AND
            else:
                break
        return left

    def _not(self) -> pd.Series:
        if self._peek() and self._peek()[0] == "NOT":
            self._consume("NOT")
            return ~self._not()
        return self._atom()

    def _atom(self) -> pd.Series:
        tok = self._peek()
        if tok is None:
            raise SlicerError("Query ended where an expression was expected.")
        kind = tok[0]
        if kind == "LPAREN":
            self._consume("LPAREN")
            inner = self._or()
            if self._peek() and self._peek()[0] == "RPAREN":
                self._consume("RPAREN")
            else:
                raise SlicerError("Missing closing parenthesis.")
            return inner
        if kind == "PHRASE":
            self._consume("PHRASE")
            return self.series.str.contains(re.escape(tok[1].casefold()), na=False)
        if kind == "WORD":
            self._consume("WORD")
            word = tok[1].casefold()
            pattern = _word_pattern(word)
            return self.series.str.contains(pattern, na=False)
        if kind == "FIELD_TERM":
            self._consume("FIELD_TERM")
            field, raw_val, is_phrase = tok[1]
            return self._eval_field(field, raw_val, is_phrase)
        raise SlicerError(f"Unexpected token {kind!r}.")

    def _eval_field(self, field: str, raw_val: str, is_phrase: bool) -> pd.Series:
        if self.df is None:
            raise SlicerError(
                f"Column-targeted term '{field}:…' requires the full corpus; "
                "this call path has text only."
            )
        cols, match_kind = FIELD_ALIASES[field]
        val = (raw_val or "").strip().casefold()
        if val == "":
            # field:<empty> means "column is blank" — useful for finding rows missing metadata.
            mask = pd.Series(False, index=self.series.index)
            for col in cols:
                colser = self._col_lower(col)
                mask = mask | (colser == "")
            return mask

        mask = pd.Series(False, index=self.series.index)
        for col in cols:
            colser = self._col_lower(col)
            if match_kind == "substring":
                mask = mask | colser.str.contains(re.escape(val), na=False)
            elif match_kind == "exact_token":
                # Accept full-field equality OR comma/space-separated membership.
                mask = mask | (colser == val) | colser.str.contains(
                    r"(?:^|[,\s;|])" + re.escape(val) + r"(?:$|[,\s;|])", regex=True, na=False
                )
            else:  # word_or_phrase (default)
                if is_phrase:
                    mask = mask | colser.str.contains(re.escape(val), na=False)
                else:
                    mask = mask | colser.str.contains(_word_pattern(val), regex=True, na=False)
        return mask


def apply_boolean_query(
    source: Union[pd.Series, pd.DataFrame],
    query: str,
    text_col: str = "text",
) -> pd.Series:
    """Apply a Brandwatch-style boolean query to a Series or DataFrame.

    - Passing a Series: only bare terms (matched against that series) are allowed;
      ``field:value`` syntax will raise SlicerError.
    - Passing a DataFrame: bare terms match against ``df[text_col]``; ``field:value``
      terms match the mapped column(s) from FIELD_ALIASES.

    Returns a boolean mask (True = row matches). An empty/whitespace-only query
    returns an all-True mask.
    """
    query = (query or "").strip()
    if isinstance(source, pd.DataFrame):
        df = source
        if text_col not in df.columns:
            raise SlicerError(f"Corpus has no {text_col!r} column.")
        series = df[text_col]
    else:
        df = None
        series = source
    if not query:
        return pd.Series(True, index=series.index)
    return _BoolParser(_tokenize(query), series, df=df).parse()


@dataclass
class SliceResult:
    matched: int
    total: int
    preview_indices: List[int]

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "total": self.total,
            "preview_indices": self.preview_indices,
        }


def run_slice(df: pd.DataFrame, query: str, text_col: str = "text", preview_n: int = 10) -> Tuple[pd.DataFrame, SliceResult]:
    """Run a query against ``df[text_col]`` and return the matching sub-DataFrame
    plus a ``SliceResult`` summary. Raises ``SlicerError`` on bad syntax.
    """
    if text_col not in df.columns:
        raise SlicerError(f"Corpus has no '{text_col}' column to query.")
    mask = apply_boolean_query(df, query, text_col=text_col)
    sub = df[mask]
    preview = sub.head(preview_n)
    idx_col = "_row_idx" if "_row_idx" in preview.columns else None
    preview_indices = (
        [int(v) for v in preview[idx_col].tolist()] if idx_col else [int(i) for i in preview.index.tolist()]
    )
    return sub, SliceResult(matched=int(len(sub)), total=int(len(df)), preview_indices=preview_indices)


# ─── Sampling & splitting ────────────────────────────────────────────────────
SAMPLE_METHODS = ("random", "stratified", "top_n", "systematic")


def _apply_base_query(df: pd.DataFrame, base_query: str) -> pd.DataFrame:
    """Pre-filter the corpus with an optional boolean query before sampling."""
    q = (base_query or "").strip()
    if not q:
        return df
    if "text" not in df.columns:
        raise SlicerError("Corpus has no 'text' column to run the base query against.")
    mask = apply_boolean_query(df, q)
    return df[mask]


def random_sample(df: pd.DataFrame, n: int, seed: int = 42) -> pd.DataFrame:
    """Uniform random sample of ``n`` rows. Returns df unchanged if n >= len(df)."""
    n = max(0, int(n))
    if n == 0 or len(df) == 0:
        return df.iloc[0:0]
    if n >= len(df):
        return df
    return df.sample(n=n, random_state=int(seed))


def stratified_sample(df: pd.DataFrame, n: int, by_col: str, seed: int = 42) -> pd.DataFrame:
    """Proportional stratified sample: preserve the distribution of ``by_col``.

    Strata with missing values are bucketed into ``"__missing__"``. Rounding is
    handled so the returned frame has exactly ``n`` rows (or the full length of
    ``df`` if ``n`` exceeds it).
    """
    n = max(0, int(n))
    if n == 0 or len(df) == 0:
        return df.iloc[0:0]
    if by_col not in df.columns:
        raise SlicerError(f"Stratify column {by_col!r} not found in the corpus.")
    if n >= len(df):
        return df

    work = df.copy()
    work["_strat_key"] = work[by_col].fillna("__missing__").astype(str)
    counts = work["_strat_key"].value_counts()
    total = int(counts.sum())
    # Largest-remainder method so quotas sum exactly to n.
    raw = {k: (c * n / total) for k, c in counts.items()}
    quotas = {k: int(math.floor(v)) for k, v in raw.items()}
    deficit = n - sum(quotas.values())
    # Hand out the remaining slots to strata with the largest fractional parts.
    remainders = sorted(raw.items(), key=lambda kv: (kv[1] - quotas[kv[0]]), reverse=True)
    i = 0
    while deficit > 0 and i < len(remainders):
        k, _ = remainders[i]
        # Don't exceed the stratum's actual size.
        if quotas[k] < int(counts[k]):
            quotas[k] += 1
            deficit -= 1
        i += 1
        if i == len(remainders) and deficit > 0:
            i = 0  # wrap around if needed (very rare)

    parts = []
    rng_state = int(seed)
    for k, take in quotas.items():
        take = min(take, int(counts[k]))
        if take <= 0:
            continue
        sub = work[work["_strat_key"] == k]
        parts.append(sub.sample(n=take, random_state=rng_state))
        rng_state += 1  # keep draws independent across strata
    if not parts:
        return df.iloc[0:0]
    out = pd.concat(parts).drop(columns=["_strat_key"]).sample(frac=1.0, random_state=int(seed))
    return out


def top_n_sample(df: pd.DataFrame, n: int, by_col: str, ascending: bool = False) -> pd.DataFrame:
    """Sort by ``by_col`` and take ``n`` rows. NaN values sort last regardless of direction."""
    n = max(0, int(n))
    if n == 0 or len(df) == 0:
        return df.iloc[0:0]
    if by_col not in df.columns:
        raise SlicerError(f"Sort column {by_col!r} not found in the corpus.")
    sorted_df = df.sort_values(by_col, ascending=ascending, na_position="last")
    return sorted_df.head(n)


def systematic_sample(df: pd.DataFrame, step: int) -> pd.DataFrame:
    """Every Nth row (step=5 → rows 0,5,10,…). Step must be ≥ 1."""
    step = max(1, int(step))
    if len(df) == 0:
        return df
    return df.iloc[::step]


def equal_chunks(df: pd.DataFrame, k: int, seed: int = 42, overlap_pct: float = 0.0) -> List[pd.DataFrame]:
    """Split ``df`` into ``k`` roughly-equal chunks after a random shuffle.

    If ``overlap_pct > 0``, a shared pool of ``overlap_pct %`` rows is added to
    every chunk (useful for inter-coder reliability checks).
    """
    k = max(1, int(k))
    if len(df) == 0:
        return [df.iloc[0:0] for _ in range(k)]
    shuffled = df.sample(frac=1.0, random_state=int(seed)).reset_index(drop=True)
    overlap_pct = max(0.0, min(99.0, float(overlap_pct)))
    n = len(shuffled)
    overlap_n = int(round(n * overlap_pct / 100.0))
    overlap_df = shuffled.iloc[:overlap_n]
    rest = shuffled.iloc[overlap_n:]

    # Split the non-overlap portion into k contiguous chunks (already shuffled,
    # so any split is random). np.array_split-style: last chunks get the extras.
    chunks: List[pd.DataFrame] = []
    size = len(rest)
    base = size // k
    extras = size % k
    start = 0
    for i in range(k):
        take = base + (1 if i < extras else 0)
        chunk = rest.iloc[start:start + take]
        start += take
        if overlap_n > 0:
            chunk = pd.concat([chunk, overlap_df])
        chunks.append(chunk)
    return chunks


def indices_of(df: pd.DataFrame) -> List[int]:
    """Return the ``_row_idx`` values for every row, or a positional fallback."""
    if "_row_idx" in df.columns:
        return [int(v) for v in df["_row_idx"].tolist()]
    return [int(i) for i in df.index.tolist()]


@dataclass
class SampleSpec:
    """Parameters used to produce a sample slice. Stored on disk for reproducibility."""
    method: str                         # one of SAMPLE_METHODS
    n: int = 200
    seed: int = 42
    by_col: Optional[str] = None        # for stratified / top_n
    ascending: bool = False             # for top_n (False = descending / largest first)
    step: int = 10                      # for systematic
    base_query: str = ""                # optional pre-filter
    dedupe_text: bool = False           # drop near-duplicate text before sampling

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SampleSpec":
        return cls(
            method=str(data.get("method", "random")),
            n=int(data.get("n", 200) or 200),
            seed=int(data.get("seed", 42) or 42),
            by_col=data.get("by_col") or None,
            ascending=bool(data.get("ascending", False)),
            step=int(data.get("step", 10) or 10),
            base_query=str(data.get("base_query", "") or ""),
            dedupe_text=bool(data.get("dedupe_text", False)),
        )


def run_sample(df: pd.DataFrame, spec: SampleSpec) -> pd.DataFrame:
    """Execute a sampling spec and return the sampled DataFrame."""
    if spec.method not in SAMPLE_METHODS:
        raise SlicerError(f"Unknown sampling method: {spec.method!r}")
    base = _apply_base_query(df, spec.base_query)
    if spec.dedupe_text and "text" in base.columns:
        norm = base["text"].fillna("").astype(str).str.lower().str.strip()
        base = base[~norm.duplicated(keep="first")]
    if spec.method == "random":
        return random_sample(base, spec.n, spec.seed)
    if spec.method == "stratified":
        if not spec.by_col:
            raise SlicerError("Stratified sampling needs a 'by_col' column.")
        return stratified_sample(base, spec.n, spec.by_col, spec.seed)
    if spec.method == "top_n":
        if not spec.by_col:
            raise SlicerError("Top-N sampling needs a 'by_col' column.")
        return top_n_sample(base, spec.n, spec.by_col, ascending=spec.ascending)
    # systematic
    return systematic_sample(base, spec.step)


def describe_sample(spec: SampleSpec) -> str:
    """Build a human-readable label used as the ``query`` field for sample slices."""
    bits: List[str] = []
    if spec.method == "random":
        bits.append(f"Random sample · n={spec.n} · seed={spec.seed}")
    elif spec.method == "stratified":
        bits.append(f"Stratified by {spec.by_col} · n={spec.n} · seed={spec.seed}")
    elif spec.method == "top_n":
        direction = "asc" if spec.ascending else "desc"
        bits.append(f"Top {spec.n} by {spec.by_col} ({direction})")
    elif spec.method == "systematic":
        bits.append(f"Every {spec.step}th row")
    if spec.base_query:
        bits.append(f'filtered by: {spec.base_query}')
    if spec.dedupe_text:
        bits.append("de-duplicated by text")
    return " · ".join(bits)


# ─── Help text (shown in the Slicer UI) ──────────────────────────────────────
SYNTAX_HELP = (
    "Brandwatch-style boolean query. Bare terms match the text column.\n\n"
    "Operators (case-insensitive):\n"
    "  AND  — both terms must appear\n"
    "  OR   — either term\n"
    "  NOT  — term must not appear\n\n"
    "Quoting: \"exact phrase\"\n"
    "Wildcards: climat* → climate, climatic, …\n"
    "Grouping: (A OR B) AND C\n"
    "Adjacent words without an operator = implicit AND.\n\n"
    "Column-targeted terms (field:value):\n"
    "  author: / handle: / name:  — author_handle / author_name\n"
    "  url: / link:               — url substring\n"
    "  hashtag: / tag:            — hashtags\n"
    "  language: / lang:          — exact code (e.g. lang:de)\n"
    "  country:                   — exact code (e.g. country:DE)\n"
    "  platform:                  — exact (e.g. platform:twitter)\n"
    "  source: / dataset:         — source_dataset substring\n"
    "  id: / post_id:             — post_id substring\n"
    "Use quotes for spaces: author:\"Greta Thunberg\"\n\n"
    "Examples:\n"
    "  climate AND (change OR warming)\n"
    '  "fake news" OR disinformation\n'
    "  covid NOT (vaccine OR mask)\n"
    "  (ISD OR extremism) AND NOT satire\n"
    "  lang:de AND climate*\n"
    "  author:realdonald AND url:youtube.com\n"
    "  hashtag:maga NOT lang:en"
)
