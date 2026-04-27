"""Monthly spend ceiling for AI runs.

Stored under AppState.settings:
    "monthly_budget_usd": 25.0   # 0 or missing = no ceiling
    "monthly_spent": {"2026-04": 1.23, ...}

Semantics (see SINGLE_USER_ROADMAP P10.8):
    80% of budget → warn in the preflight response
    100% → hard-block unless the caller confirms by typing the budget
           amount exactly (front-end widget).
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict

# Guards compound read-modify-write on settings["monthly_spent"]. Two concurrent
# AI runs hitting record_spend simultaneously used to clobber each other.
_BUDGET_LOCK = threading.Lock()


def _ym(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now.year:04d}-{now.month:02d}"


def get_budget(settings: Dict[str, Any]) -> float:
    try:
        v = float(settings.get("monthly_budget_usd") or 0)
        return max(0.0, v)
    except (TypeError, ValueError):
        return 0.0


def get_spent(settings: Dict[str, Any]) -> float:
    month = _ym()
    spent = (settings.get("monthly_spent") or {}).get(month) or 0.0
    try:
        return float(spent)
    except (TypeError, ValueError):
        return 0.0


def check(settings: Dict[str, Any], estimated_usd: float) -> Dict[str, Any]:
    """Return a budget check envelope for the preflight response.

    `spent` here is the *effective* spent — recorded spend plus currently-held
    reservations — so a preflight during a running job can't mislead the user
    into starting a second run that would blow the ceiling."""
    budget = get_budget(settings)
    spent = effective_spent(settings)
    est = max(0.0, float(estimated_usd or 0.0))
    out = {
        "budget_usd": round(budget, 4),
        "spent_usd": round(spent, 4),
        "estimated_usd": round(est, 4),
        "remaining_usd": round(max(0.0, budget - spent), 4) if budget else None,
        "month": _ym(),
        "status": "ok",
        "message": "",
        "requires_override": False,
    }
    if budget <= 0:
        out["status"] = "disabled"
        out["message"] = "No monthly budget set. You can add one in Settings."
        return out
    post_spent = spent + est
    if post_spent >= budget:
        out["status"] = "block"
        out["requires_override"] = True
        out["message"] = (
            f"This run would bring your spend to ${post_spent:.2f}, "
            f"at or over the ${budget:.2f} monthly ceiling. "
            "To proceed, type the exact budget amount to confirm."
        )
        return out
    if post_spent >= 0.8 * budget:
        out["status"] = "warn"
        out["message"] = (
            f"Heads up: after this run you would have spent "
            f"${post_spent:.2f} of your ${budget:.2f} monthly ceiling "
            f"(~{int(post_spent/budget*100)}%)."
        )
        return out
    return out


def record_spend(settings: Dict[str, Any], usd: float) -> Dict[str, Any]:
    """Add `usd` to this month's spent bucket; return the updated dict-in-place.

    Thread-safe: two workers recording concurrent spend used to race on the
    read-then-write of monthly_spent[month] and could lose one delta. The lock
    serializes the RMW window."""
    if usd <= 0:
        return settings
    month = _ym()
    with _BUDGET_LOCK:
        bucket = settings.get("monthly_spent")
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[month] = round(float(bucket.get(month) or 0.0) + float(usd), 6)
        settings["monthly_spent"] = bucket
    return settings


# ─── Reservations (P10.8 reserve-before-call) ───────────────────────────────
# Concurrent AI runs used to be able to pass preflight individually, then both
# spend past the ceiling because no reservation was held. The fix: reserve the
# estimated cost atomically before the call, then reconcile against the actual
# cost after. Reservations live in settings["monthly_reserved"] per-month and
# are deducted when the run completes.

def _current_reservations(settings: Dict[str, Any]) -> float:
    month = _ym()
    bucket = (settings.get("monthly_reserved") or {}).get(month) or 0.0
    try:
        return max(0.0, float(bucket))
    except (TypeError, ValueError):
        return 0.0


def effective_spent(settings: Dict[str, Any]) -> float:
    """Already-spent + currently-reserved. Used by the preflight check so that
    concurrent runs can't all pass a budget check individually."""
    return get_spent(settings) + _current_reservations(settings)


def reserve_spend(settings: Dict[str, Any], estimated_usd: float) -> bool:
    """Atomically reserve `estimated_usd` against this month's budget.

    Returns True if the reservation fits under the ceiling (or no ceiling is set),
    False if it would exceed the cap. Call `release_reservation()` or
    `commit_reservation()` when the run ends. A failed reservation does NOT
    modify settings."""
    est = max(0.0, float(estimated_usd or 0.0))
    if est <= 0:
        return True
    budget = get_budget(settings)
    month = _ym()
    with _BUDGET_LOCK:
        if budget > 0:
            spent = get_spent(settings)
            reserved = _current_reservations(settings)
            if spent + reserved + est > budget:
                return False
        res_bucket = settings.get("monthly_reserved")
        if not isinstance(res_bucket, dict):
            res_bucket = {}
        res_bucket[month] = round(float(res_bucket.get(month) or 0.0) + est, 6)
        settings["monthly_reserved"] = res_bucket
    return True


def release_reservation(settings: Dict[str, Any], estimated_usd: float) -> None:
    """Undo a prior `reserve_spend` (e.g. when a run failed before any API call)."""
    est = max(0.0, float(estimated_usd or 0.0))
    if est <= 0:
        return
    month = _ym()
    with _BUDGET_LOCK:
        res_bucket = settings.get("monthly_reserved")
        if not isinstance(res_bucket, dict):
            return
        cur = float(res_bucket.get(month) or 0.0)
        res_bucket[month] = round(max(0.0, cur - est), 6)
        settings["monthly_reserved"] = res_bucket


def commit_reservation(
    settings: Dict[str, Any],
    estimated_usd: float,
    actual_usd: float,
) -> Dict[str, Any]:
    """Replace a reservation with the actual spend. Call once per run on success.

    Atomic: releases the reservation AND records actual spend under the same lock,
    so no gap lets a concurrent run sneak through."""
    est = max(0.0, float(estimated_usd or 0.0))
    act = max(0.0, float(actual_usd or 0.0))
    month = _ym()
    with _BUDGET_LOCK:
        # Release reservation
        res_bucket = settings.get("monthly_reserved")
        if isinstance(res_bucket, dict):
            cur = float(res_bucket.get(month) or 0.0)
            res_bucket[month] = round(max(0.0, cur - est), 6)
            settings["monthly_reserved"] = res_bucket
        # Record actual spend (inlined — we already hold the lock)
        if act > 0:
            bucket = settings.get("monthly_spent")
            if not isinstance(bucket, dict):
                bucket = {}
            bucket[month] = round(float(bucket.get(month) or 0.0) + act, 6)
            settings["monthly_spent"] = bucket
    return settings


def validate_override(settings: Dict[str, Any], override: str | float | None) -> bool:
    """User must type the exact budget amount (as number) to bypass a block."""
    budget = get_budget(settings)
    if budget <= 0:
        return False
    try:
        return abs(float(override) - budget) < 1e-6
    except (TypeError, ValueError):
        return False
