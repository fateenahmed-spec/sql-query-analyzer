"""Build post-order animation steps from normalized plan trees."""

from __future__ import annotations

from typing import Any


_SCAN_OPERATORS = {
    "Table scan",
    "Index scan",
    "Index range scan",
    "Index lookup",
    "Single-row index lookup",
    "Covering index lookup",
    "Covering index scan",
    "Single-row covering index lookup",
}


def operator_family(operator: str) -> str:
    """Classify the physical plan operator, independently of SQL logic stages."""
    normalized = str(operator or "").casefold()
    if operator in _SCAN_OPERATORS or normalized == "rows fetched before execution":
        return "scan"
    if normalized == "filter":
        return "filter"
    if "join" in normalized:
        return "join"
    if normalized.startswith("aggregate") or normalized.startswith("group aggregate") or normalized.startswith("groupaggregate"):
        return "aggregate"
    if normalized.startswith("window aggregate") or normalized == "window":
        return "window"
    if normalized == "sort":
        return "sort"
    if normalized == "limit":
        return "limit"
    if normalized.startswith("materialize"):
        return "materialize"
    if normalized == "hash":
        return "hash"
    if normalized == "zero rows":
        return "empty"
    return "other"


def _action(operator: str) -> str:
    family = operator_family(operator)
    return family if family in {"scan", "filter", "join", "aggregate", "window", "sort", "limit", "materialize", "empty"} else "other"


def build_animation_steps(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Create one child-before-parent animation step for every plan node."""
    steps: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], parent_id: str | None) -> None:
        for child in node.get("children", []):
            visit(child, node["id"])
        steps.append(
            {
                "step": len(steps) + 1,
                "node_id": node["id"],
                "parent_id": parent_id,
                "action": _action(str(node.get("operator") or "")),
                "layer": "physical execution-plan operator",
                "operator_family": operator_family(str(node.get("operator") or "")),
                "operator": str(node.get("operator") or ""),
                "rows_out": node.get("rows_total"),
                "loops": node.get("loops"),
                "never_executed": bool(node.get("never_executed")),
            }
        )

    visit(tree, None)
    return steps
