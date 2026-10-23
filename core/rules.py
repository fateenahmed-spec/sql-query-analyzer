"""Analyze normalized plans and emit actionable warnings."""

from __future__ import annotations

from typing import Any


Warning = dict[str, Any]

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _walk(node: dict[str, Any], ancestors: tuple[dict[str, Any], ...] = ()):
    yield node, ancestors
    for child in node.get("children", []):
        yield from _walk(child, ancestors + (node,))


def _real_table_scan(node: dict[str, Any]) -> bool:
    details = str(node.get("details") or "").casefold()
    return node.get("operator") == "Table scan" and "<temporary>" not in details and "<subquery" not in details


def _warning(
    code: str,
    severity: str,
    node: dict[str, Any],
    title: str,
    message: str,
    suggestion: str | None,
) -> Warning:
    return {
        "code": code,
        "severity": severity,
        "node_id": node["id"],
        "title": title,
        "message": message,
        "suggestion": suggestion,
    }


def _id_number(node_id: str) -> int:
    try:
        return int(node_id.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return 0


def analyze_rules(tree: dict[str, Any]) -> list[Warning]:
    """Return warnings for a plan without changing any part of the plan."""
    nodes = list(_walk(tree))
    warnings: list[Warning] = []

    for node, ancestors in nodes:
        operator = str(node.get("operator") or "")
        details = str(node.get("details") or "")
        rows_total = node.get("rows_total")
        loops = node.get("loops")

        if _real_table_scan(node):
            if loops is not None and loops > 1:
                warnings.append(
                    _warning(
                        "INNER_SCAN_IN_LOOP",
                        "high",
                        node,
                        "Table scan repeated in a loop",
                        f"The table scan on {details.removeprefix('on ').strip() or 'a table'} runs {loops} times inside a loop.",
                        "Add or use an index that lets the repeated lookup read fewer rows.",
                    )
                )
            elif loops == 1 and rows_total is not None and rows_total > 1000:
                severity = "high" if rows_total > 10000 else "medium"
                warnings.append(
                    _warning(
                        "FULL_SCAN",
                        severity,
                        node,
                        "Large table scan",
                        f"The table scan on {details.removeprefix('on ').strip() or 'a table'} reads {rows_total} rows.",
                        "Consider an index that matches the query's filters or join conditions.",
                    )
                )

        if operator == "Sort":
            bounded_top_n = "limit input to" in details.casefold()
            under_limit = any(parent.get("operator") == "Limit" for parent in ancestors)
            input_rows = node["children"][0].get("rows_total") if node.get("children") else None
            if input_rows is not None and input_rows > 1000 and not (bounded_top_n or under_limit):
                warnings.append(
                    _warning(
                        "LARGE_SORT",
                        "medium",
                        node,
                        "Large sort input",
                        f"The Sort operator processes {input_rows} rows before ordering them.",
                        "Reduce the rows reaching the sort or add an index that supports the requested order.",
                    )
                )

        estimated_rows = node.get("estimated_rows")
        actual_rows = node.get("actual_rows")
        if not node.get("never_executed") and estimated_rows is not None and actual_rows is not None:
            ratio = max(estimated_rows, actual_rows) / max(min(estimated_rows, actual_rows), 1)
            if ratio >= 10 and abs(estimated_rows - actual_rows) >= 100:
                severity = "medium" if ratio >= 100 else "low"
                warnings.append(
                    _warning(
                        "ESTIMATE_MISMATCH",
                        severity,
                        node,
                        "Row estimate differs from actual rows",
                        f"The {operator or 'plan'} estimate is {estimated_rows} rows per loop, while it produced {actual_rows}.",
                        "Refresh table statistics and review the predicates used by this operator.",
                    )
                )

        if operator == "Filter" and node.get("children"):
            child_rows = node["children"][0].get("rows_total")
            if child_rows is not None and child_rows >= 1000 and rows_total is not None and rows_total < child_rows * 0.1:
                warnings.append(
                    _warning(
                        "FILTER_DISCARDS_MOST",
                        "low",
                        node,
                        "Filter discards most rows",
                        f"The Filter operator keeps {rows_total} of {child_rows} rows from its input.",
                        "Check whether an index can apply this filter before reading the full input.",
                    )
                )

        if "temporary table" in operator.casefold():
            warnings.append(
                _warning(
                    "TEMP_TABLE",
                    "low",
                    node,
                    "Temporary table used",
                    f"The {operator} operator uses a temporary table.",
                    "Review grouping and ordering requirements to see whether an index can avoid the temporary table.",
                )
            )

        if node.get("never_executed"):
            warnings.append(
                _warning(
                    "NEVER_EXECUTED",
                    "info",
                    node,
                    "Operator was never executed",
                    f"The {operator or 'plan'} operator was not executed for this query.",
                    None,
                )
            )

    non_root = [node for node, _ in nodes if node is not tree]
    if non_root:
        hotspot = max(non_root, key=lambda item: item.get("self_time_pct") or 0)
        if (hotspot.get("self_time_pct") or 0) >= 40:
            operator = str(hotspot.get("operator") or "operator")
            warnings.append(
                _warning(
                    "HOTSPOT",
                    "medium",
                    hotspot,
                    "Largest time hotspot",
                    f"The {operator} operator has the highest self time at {hotspot['self_time_pct']:.2f}% of the plan.",
                    "Focus query tuning on this operator and the rows it processes.",
                )
            )

    warnings.sort(
        key=lambda item: (
            _SEVERITY_ORDER.get(item["severity"], len(_SEVERITY_ORDER)),
            _id_number(item["node_id"]),
        )
    )
    return warnings


def annotate_tree(tree: dict[str, Any], warnings: list[Warning]) -> dict[str, Any]:
    """Replace each node's warning-code list with the supplied annotations."""
    codes_by_node: dict[str, list[str]] = {}
    for warning in warnings:
        codes_by_node.setdefault(warning["node_id"], []).append(warning["code"])

    for node, _ in _walk(tree):
        node["warnings"] = codes_by_node.get(node["id"], []).copy()
    return tree
