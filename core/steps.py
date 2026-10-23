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


def _action(operator: str) -> str:
    if operator in _SCAN_OPERATORS:
        return "scan"
    if operator == "Filter":
        return "filter"
    if "join" in operator.casefold():
        return "join"
    if operator.startswith("Aggregate") or operator == "Group aggregate":
        return "aggregate"
    if operator == "Sort":
        return "sort"
    if operator == "Limit":
        return "limit"
    return "other"


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
                "rows_out": node.get("rows_total"),
                "loops": node.get("loops"),
                "never_executed": bool(node.get("never_executed")),
            }
        )

    visit(tree, None)
    return steps
