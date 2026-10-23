"""Coordinate plan loading, parsing, rules, and response construction."""

from __future__ import annotations

import json
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_plan_text(plan_id: str) -> tuple[str, str]:
    """Load a captured plan, falling back to its checked-in demo plan."""
    query_file = _PROJECT_ROOT / "demo" / "queries.json"
    with query_file.open("r", encoding="utf-8") as handle:
        queries = json.load(handle)
    valid_ids = {query.get("id") for query in queries if isinstance(query, dict)}
    if plan_id not in valid_ids:
        raise KeyError(f"Unknown plan id: {plan_id!r}")

    captured = _PROJECT_ROOT / "demo" / "plans" / f"{plan_id}.txt"
    if captured.is_file():
        return captured.read_text(encoding="utf-8"), "captured"

    fallback = _PROJECT_ROOT / "demo" / "plans_fallback" / f"{plan_id}.txt"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8"), "fallback"

    raise KeyError(f"No captured or fallback plan exists for valid plan id {plan_id!r}")
