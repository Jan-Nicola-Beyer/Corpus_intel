"""APA / LaTeX table emitters (P13).

Reformat pandas DataFrames or pre-computed dicts into publication-ready tables.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

log = logging.getLogger("corpus_intel.tables")


def _escape_latex(s: Any) -> str:
    t = str(s)
    for ch, esc in [("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                    ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                    ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")]:
        t = t.replace(ch, esc)
    return t


def df_to_latex(df: pd.DataFrame, *, caption: str = "", label: str = "",
                columns: Optional[Sequence[str]] = None, round_to: int = 3) -> str:
    """Render a DataFrame as an APA-style LaTeX booktabs table."""
    if columns:
        df = df[[c for c in columns if c in df.columns]]
    out = df.copy()
    for c in out.select_dtypes(include="number").columns:
        out[c] = out[c].round(round_to)

    cols = list(out.columns)
    align = "l" + "r" * (len(cols) - 1) if cols else "l"
    lines = []
    lines.append("\\begin{table}[h]")
    lines.append("\\centering")
    if caption:
        lines.append(f"\\caption{{{_escape_latex(caption)}}}")
    if label:
        lines.append(f"\\label{{{label}}}")
    lines.append(f"\\begin{{tabular}}{{{align}}}")
    lines.append("\\toprule")
    lines.append(" & ".join(_escape_latex(c) for c in cols) + " \\\\")
    lines.append("\\midrule")
    for _, row in out.iterrows():
        lines.append(" & ".join(_escape_latex(row[c]) for c in cols) + " \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    lines.append("\\end{table}")
    return "\n".join(lines)


def df_to_apa_markdown(df: pd.DataFrame, *, caption: str = "", round_to: int = 3) -> str:
    """Render a DataFrame as an APA-style Markdown table (no vertical rules)."""
    if df is None or df.empty:
        return f"*{caption}* — (empty)"
    out = df.copy()
    for c in out.select_dtypes(include="number").columns:
        out[c] = out[c].round(round_to)
    cols = list(out.columns)
    lines = []
    if caption:
        lines.append(f"**Table:** *{caption}*")
        lines.append("")
    lines.append("| " + " | ".join(str(c) for c in cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
    for _, row in out.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
    return "\n".join(lines)
