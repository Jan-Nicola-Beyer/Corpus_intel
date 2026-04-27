"""Network analysis (P12) — build a graph from interactions/co-mentions.

Two modes:
1. `user_interaction_graph(df)`: edges from replies / retweets (source_user → target_user)
2. `co_mention_graph(df)`: two-user edges if both are mentioned in the same row

Output is a JSON-ready adjacency list + basic centrality metrics.

Pure Python; uses `networkx` if available (for centrality) but works without.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

log = logging.getLogger("corpus_intel.network")

_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,30})")


def _extract_mentions(text: str) -> List[str]:
    return [m.lower() for m in _MENTION_RE.findall(str(text or ""))]


def _try_networkx():
    try:
        import networkx as nx  # type: ignore
        return nx
    except ImportError:
        return None


def _empty_graph(reason: str, **diag) -> Dict[str, Any]:
    out: Dict[str, Any] = {"nodes": [], "edges": [], "metrics": {}, "reason": reason}
    out.update(diag)
    return out


def user_interaction_graph(
    df: pd.DataFrame,
    *,
    source_col: str = "author_handle",
    target_col: str = "in_reply_to",
) -> Dict[str, Any]:
    missing = [c for c in (source_col, target_col) if c not in df.columns]
    if missing:
        return _empty_graph(
            f"missing_columns:{','.join(missing)}",
            hint=f"Interaction mode needs '{source_col}' and '{target_col}'. "
                 f"This corpus only has: {sorted([c for c in df.columns if isinstance(c, str)])[:30]}",
        )
    pairs = df[[source_col, target_col]].dropna()
    if pairs.empty:
        return _empty_graph(
            f"no_rows_with_both:{source_col},{target_col}",
            hint="No rows have both columns populated — this corpus likely has no reply data.",
        )
    edges: Counter = Counter()
    for _, row in pairs.iterrows():
        src = str(row[source_col]).lower().strip()
        tgt = str(row[target_col]).lower().strip()
        if src and tgt and src != tgt:
            edges[(src, tgt)] += 1
    if not edges:
        return _empty_graph("no_valid_edges",
                            hint="All pairs were self-loops or empty strings.")
    return _finalise_graph(edges)


def co_mention_graph(
    df: pd.DataFrame,
    *,
    text_col: str = "text",
    min_edge_weight: int = 2,
) -> Dict[str, Any]:
    if text_col not in df.columns:
        return _empty_graph(f"missing_columns:{text_col}",
                            hint=f"Co-mention mode needs a '{text_col}' column.")
    edges: Counter = Counter()
    rows_with_any_mention = 0
    for txt in df[text_col].fillna(""):
        mentions = sorted(set(_extract_mentions(txt)))
        if mentions:
            rows_with_any_mention += 1
        for i, a in enumerate(mentions):
            for b in mentions[i+1:]:
                key = (a, b) if a < b else (b, a)
                edges[key] += 1
    edges = Counter({k: v for k, v in edges.items() if v >= min_edge_weight})
    if not edges:
        return _empty_graph(
            "no_edges_above_threshold",
            rows_with_any_mention=rows_with_any_mention,
            min_edge_weight=min_edge_weight,
            hint=(f"{rows_with_any_mention} rows contain @-mentions but no user-pair "
                  f"co-occurred at least {min_edge_weight} times. Lower min_edge_weight to 1 to see all pairs."),
        )
    return _finalise_graph(edges, directed=False)


def _finalise_graph(edges: Counter, *, directed: bool = True) -> Dict[str, Any]:
    nodes = set()
    for (a, b) in edges:
        nodes.add(a); nodes.add(b)

    node_list = sorted(nodes)
    edge_list = [{"src": a, "tgt": b, "weight": int(w)} for (a, b), w in edges.items()]

    metrics: Dict[str, Any] = {}
    nx = _try_networkx()
    if nx is not None and node_list:
        g = nx.DiGraph() if directed else nx.Graph()
        g.add_nodes_from(node_list)
        for e in edge_list:
            g.add_edge(e["src"], e["tgt"], weight=e["weight"])
        try:
            deg = dict(g.degree(weight="weight"))
            metrics["top_degree"] = sorted(deg.items(), key=lambda x: -x[1])[:20]
            if len(node_list) <= 5000:
                metrics["top_betweenness"] = sorted(
                    nx.betweenness_centrality(g, weight="weight", k=min(200, len(node_list))).items(),
                    key=lambda x: -x[1],
                )[:20]
        except Exception as e:
            log.warning("centrality failed: %s", e)

    return {
        "nodes": [{"id": n} for n in node_list],
        "edges": edge_list,
        "metrics": metrics,
        "directed": directed,
    }
