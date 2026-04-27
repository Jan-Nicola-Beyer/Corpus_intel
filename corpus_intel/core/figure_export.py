"""Publication-grade figure export (P13) — 300 dpi PNG + SVG via matplotlib.

Takes a Chart.js-style config (labels + datasets) and renders it server-side
so the user can drop a figure straight into a paper without re-styling.

Pure Python via matplotlib; no browser screenshot.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

import numpy as np

log = logging.getLogger("corpus_intel.figures")


def _lazy_mpl():
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    return plt


def _series_colors(n: int) -> List[str]:
    # Reuse ISD accent palette.
    base = ["#C8175D", "#0f0f0f", "#666666", "#FFB703", "#2a9d8f", "#457b9d", "#8d99ae"]
    return [base[i % len(base)] for i in range(n)]


def render_bar(labels: List[str], datasets: List[Dict[str, Any]],
               *, title: str = "", xlabel: str = "", ylabel: str = "",
               horizontal: bool = False, fmt: str = "png",
               dpi: int = 300, figsize: tuple = (10, 6)) -> bytes:
    """Render a bar chart matching Chart.js-style config."""
    plt = _lazy_mpl()
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    colors = _series_colors(len(datasets))
    width = 0.8 / max(1, len(datasets))
    positions = np.arange(len(labels))

    for i, ds in enumerate(datasets):
        data = ds.get("data") or []
        label = ds.get("label") or f"series {i+1}"
        if horizontal:
            ax.barh(positions + i * width, data, height=width, label=label, color=colors[i])
        else:
            ax.bar(positions + i * width, data, width=width, label=label, color=colors[i])

    if horizontal:
        ax.set_yticks(positions + width * (len(datasets) - 1) / 2)
        ax.set_yticklabels(labels)
    else:
        ax.set_xticks(positions + width * (len(datasets) - 1) / 2)
        ax.set_xticklabels(labels, rotation=30, ha="right")

    if title: ax.set_title(title, fontsize=13, weight="bold")
    if xlabel: ax.set_xlabel(xlabel)
    if ylabel: ax.set_ylabel(ylabel)
    if len(datasets) > 1: ax.legend()
    ax.grid(axis="y" if not horizontal else "x", alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def render_line(labels: List[str], datasets: List[Dict[str, Any]],
                *, title: str = "", xlabel: str = "", ylabel: str = "",
                fmt: str = "png", dpi: int = 300, figsize: tuple = (10, 6)) -> bytes:
    plt = _lazy_mpl()
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    colors = _series_colors(len(datasets))
    for i, ds in enumerate(datasets):
        data = ds.get("data") or []
        label = ds.get("label") or f"series {i+1}"
        ax.plot(range(len(data)), data, label=label, color=colors[i], linewidth=2, marker="o", markersize=3)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    if title: ax.set_title(title, fontsize=13, weight="bold")
    if xlabel: ax.set_xlabel(xlabel)
    if ylabel: ax.set_ylabel(ylabel)
    if len(datasets) > 1: ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def render_pie(labels: List[str], values: List[float],
               *, title: str = "", fmt: str = "png", dpi: int = 300,
               figsize: tuple = (7, 7)) -> bytes:
    plt = _lazy_mpl()
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    colors = _series_colors(len(labels))
    ax.pie(values, labels=labels, colors=colors, autopct="%1.1f%%", startangle=90)
    if title: ax.set_title(title, fontsize=13, weight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
