"""Startup health check for Claude API apps.

Validates ANTHROPIC_API_KEY, checks Claude model IDs referenced in the source
tree against Anthropic's live /v1/models endpoint, verifies critical imports,
required paths, and (when packaging is available) requirements.txt pinning.

Behavior:
- One-line log summary on startup ([Health:<app>] OK / WARN / FAIL: ...).
- Raises SystemExit when ANTHROPIC_API_KEY is missing OR every referenced
  Claude model is unrecognized by the live API. Other issues warn-and-continue.
- Live model list cached at ~/.claude/model_cache.json with 24h TTL → ~1
  API call/day across all apps that share the cache file.

Public:
- run_startup_check(...) -> dict
- health_report() -> dict   (for /health endpoints)

Per-app constraint: do NOT auto-substitute deprecated models. We warn and
suggest a replacement. Operator applies the change by hand. (Reinforces the
"Model routing — do not deviate without asking" rule in each app's CLAUDE.md.)
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

CACHE_PATH = Path.home() / ".claude" / "model_cache.json"
CACHE_TTL = 24 * 60 * 60
MODELS_URL = "https://api.anthropic.com/v1/models?limit=200"
ANTHROPIC_VERSION_HEADER = "2023-06-01"
MODEL_LITERAL_RE = re.compile(r"claude-[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?")
SCAN_EXTS = (".py", ".yaml", ".yml")
SCAN_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "dist", "build", "static", "venv", ".venv"}

_LAST_REPORT: Dict[str, Any] = {"status": "unknown", "checks": [], "summary": "no check run yet"}


def _now() -> float:
    return time.time()


# ── Live-model fetch with 24h disk cache ─────────────────────────────────────
def _load_cache() -> Optional[List[str]]:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if _now() - raw.get("fetched_at", 0) > CACHE_TTL:
        return None
    models = raw.get("models")
    if isinstance(models, list) and models:
        return [str(m) for m in models]
    return None


def _store_cache(models: List[str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": _now(), "models": models}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("Could not write model cache: %s", exc)


def _fetch_live_models(api_key: str) -> List[str]:
    req = urllib.request.Request(
        MODELS_URL,
        headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION_HEADER},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return [item["id"] for item in payload.get("data", []) if "id" in item]


def _get_live_models(api_key: str) -> Tuple[List[str], str]:
    """Return (models, source). source ∈ {'cache','live','stale-cache','unavailable'}."""
    cached = _load_cache()
    if cached is not None:
        return cached, "cache"
    try:
        live = _fetch_live_models(api_key)
        _store_cache(live)
        return live, "live"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("Could not fetch /v1/models: %s", exc)
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw.get("models"), list) and raw["models"]:
            return [str(m) for m in raw["models"]], "stale-cache"
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return [], "unavailable"


# ── Source scan for claude-* literals ────────────────────────────────────────
def _iter_source_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(SCAN_EXTS):
                yield Path(dirpath) / fn


def _scan_models(root: Path) -> Dict[str, List[str]]:
    """Return {model_id: [relative_paths]}. Suppresses prefix-only matches."""
    found: Dict[str, set] = {}
    for path in _iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in MODEL_LITERAL_RE.findall(text):
            if match.count("-") < 2:
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            found.setdefault(match, set()).add(rel)

    # Drop any literal that is a strict prefix of another scanned literal
    # (e.g. 'claude-haiku-4-5' when 'claude-haiku-4-5-20251001' is also
    # present — usually a display label, not an API call).
    keys = set(found)
    redundant = {m for m in keys if any(o != m and o.startswith(m + "-") for o in keys)}
    return {m: sorted(found[m]) for m in keys - redundant}


def _suggest_replacement(broken: str, live: List[str]) -> Optional[str]:
    """Pick the highest-named live model in the same family (e.g. claude-sonnet-)."""
    if not live:
        return None
    parts = broken.split("-")
    if len(parts) < 3:
        return None
    family_prefix = "-".join(parts[:2]) + "-"
    candidates = [m for m in live if m.startswith(family_prefix)]
    if not candidates:
        return None
    return sorted(candidates)[-1]


# ── Version-pinning check ────────────────────────────────────────────────────
def _check_versions(requirements_file: Path) -> List[Dict[str, Any]]:
    try:
        from importlib.metadata import PackageNotFoundError, version as get_version
    except ImportError:
        return [{"name": "version-check", "status": "skip", "detail": "importlib.metadata unavailable"}]
    try:
        from packaging.requirements import Requirement
        from packaging.version import InvalidVersion
    except ImportError:
        return [{"name": "version-check", "status": "skip", "detail": "packaging library not installed"}]

    try:
        lines = requirements_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [{"name": "version-check", "status": "skip", "detail": f"cannot read {requirements_file}: {exc}"}]

    results: List[Dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        try:
            installed = get_version(req.name)
        except PackageNotFoundError:
            results.append({"name": req.name, "status": "missing", "detail": str(req.specifier) or "any"})
            continue
        if not req.specifier:
            results.append({"name": req.name, "status": "ok", "detail": f"installed {installed}"})
            continue
        try:
            if req.specifier.contains(installed, prereleases=True):
                results.append({"name": req.name, "status": "ok", "detail": f"installed {installed} satisfies {req.specifier}"})
            else:
                results.append({
                    "name": req.name,
                    "status": "outdated",
                    "detail": f"installed {installed}, requires {req.specifier}",
                })
        except InvalidVersion:
            continue
    return results


# ── Public API ───────────────────────────────────────────────────────────────
def run_startup_check(
    *,
    app_name: str,
    app_root: str,
    required_imports: List[str],
    required_paths: List[str],
    requirements_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Run all startup checks. Logs summary and returns the report. Hard-fails on
    missing API key or all models broken (raises SystemExit)."""
    global _LAST_REPORT
    checks: List[Dict[str, Any]] = []
    hard_fail = False

    # 1. API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        checks.append({"category": "api_key", "status": "fail", "detail": "ANTHROPIC_API_KEY not set"})
        hard_fail = True
    else:
        checks.append({"category": "api_key", "status": "ok", "detail": "set"})

    # 2. Models (only if we have a key)
    if api_key:
        referenced = _scan_models(Path(app_root))
        live, source = _get_live_models(api_key)
        if not referenced:
            checks.append({"category": "model", "status": "ok", "detail": "no claude-* literals found"})
        elif not live:
            checks.append({
                "category": "model", "status": "warn",
                "detail": f"could not fetch live model list ({source}); skipping verification",
            })
        else:
            live_set = set(live)
            deprecated = [
                {"model": m, "suggestion": _suggest_replacement(m, live), "files": p}
                for m, p in referenced.items() if m not in live_set
            ]
            all_broken = bool(deprecated) and len(deprecated) == len(referenced)
            if all_broken:
                hard_fail = True
            for d in deprecated:
                sug = f" -> try {d['suggestion']}" if d["suggestion"] else ""
                checks.append({
                    "category": "model",
                    "status": "fail" if all_broken else "warn",
                    "detail": f"{d['model']} not listed by Anthropic{sug} (in: {', '.join(d['files'])})",
                })
            ok_count = len(referenced) - len(deprecated)
            if ok_count:
                checks.append({
                    "category": "model", "status": "ok",
                    "detail": f"{ok_count}/{len(referenced)} model(s) verified ({source})",
                })
    else:
        checks.append({"category": "model", "status": "skip", "detail": "skipped (no API key)"})

    # 3. Imports
    for mod in required_imports:
        try:
            importlib.import_module(mod)
            checks.append({"category": "import", "status": "ok", "detail": mod})
        except Exception as exc:
            checks.append({"category": "import", "status": "warn", "detail": f"{mod}: {exc}"})

    # 4. Paths
    for p in required_paths:
        path = Path(p)
        if path.exists():
            checks.append({"category": "path", "status": "ok", "detail": str(path)})
        else:
            try:
                path.mkdir(parents=True, exist_ok=True)
                checks.append({"category": "path", "status": "ok", "detail": f"{path} (created)"})
            except OSError as exc:
                checks.append({"category": "path", "status": "warn", "detail": f"{path}: {exc}"})

    # 5. Versions
    if requirements_file:
        for ver in _check_versions(Path(requirements_file)):
            cat_status = "warn" if ver["status"] in ("missing", "outdated") else (
                "skip" if ver["status"] == "skip" else "ok"
            )
            checks.append({
                "category": "version",
                "status": cat_status,
                "detail": f"{ver['name']}: {ver['status']} - {ver.get('detail', '')}",
            })

    # ── Summary ──────────────────────────────────────────────────────────────
    if hard_fail:
        status = "fail"
    elif any(c["status"] == "warn" for c in checks):
        status = "warn"
    else:
        status = "ok"

    if status == "ok":
        summary = f"[Health:{app_name}] OK ({len(checks)} checks)"
    else:
        bad = [c for c in checks if c["status"] in ("warn", "fail")]
        details = "; ".join(c["detail"] for c in bad[:4])
        more = f"; +{len(bad) - 4} more" if len(bad) > 4 else ""
        summary = f"[Health:{app_name}] {status.upper()}: {details}{more}"

    if status == "ok":
        log.info(summary)
    else:
        log.warning(summary)

    report = {
        "app": app_name,
        "status": status,
        "checks": checks,
        "summary": summary,
        "timestamp": _now(),
    }
    _LAST_REPORT = report

    if hard_fail:
        print(summary, file=sys.stderr)
        raise SystemExit(f"Health check failed: {summary}")
    return report


def health_report() -> Dict[str, Any]:
    """Return the most recent startup report (for /health endpoints)."""
    return dict(_LAST_REPORT)
