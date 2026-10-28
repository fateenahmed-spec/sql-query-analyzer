"""Parse MySQL EXPLAIN ANALYZE output into normalized plan nodes."""

from __future__ import annotations

import re
from typing import Any


class ParseError(ValueError):
    """A malformed plan line, with its original line number and text."""

    def __init__(self, message: str, line_no: int, line: str) -> None:
        self.line_no = line_no
        self.line = line
        self.offending_line = line
        super().__init__(f"{message} (line {line_no}: {line!r})")


_NUMBER = r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_NODE_LINE = re.compile(r"^( *)-> (.*)$")
_NEVER_EXECUTED = re.compile(r"\s+\(never executed\)$")
_ACTUAL = re.compile(
    rf"\s+\(actual time=({_NUMBER})\.\.({_NUMBER}) rows=({_NUMBER}) loops=({_NUMBER})\)$"
)
_COST = re.compile(rf"\s+\(cost=({_NUMBER}(?:\.\.{_NUMBER})?) rows=({_NUMBER})\)$")

_OPERATORS = (
    "Single-row covering index lookup",
    "Single-row index lookup",
    "Covering index lookup",
    "Covering index scan",
    "Index range scan",
    "Index lookup",
    "Index scan",
    "Table scan",
    "Nested loop inner join",
    "Nested loop left join",
    "Nested loop semijoin",
    "Nested loop antijoin",
    "Inner hash join",
    "Left hash join",
    "Aggregate using temporary table",
    "Group aggregate",
    "Aggregate",
    "Materialize with deduplication",
    "Materialize",
    "Filter",
    "Sort",
    "Limit",
    "Hash",
    "Zero rows",
    "Rows fetched before execution",
)
_OPERATORS = tuple(sorted(_OPERATORS, key=len, reverse=True))


def _number(value: str) -> int | float:
    parsed = float(value) if "." in value or "e" in value.casefold() else int(value)
    if isinstance(parsed, float) and parsed.is_integer():
        return int(parsed)
    return parsed


def _split_stats(body: str) -> tuple[str, dict[str, Any]]:
    """Remove only recognized statistics at the end of a node body."""
    remaining = body.rstrip()
    never_executed = bool(_NEVER_EXECUTED.search(remaining))
    if never_executed:
        remaining = _NEVER_EXECUTED.sub("", remaining).rstrip()

    actual_match = _ACTUAL.search(remaining)
    if actual_match:
        remaining = _ACTUAL.sub("", remaining).rstrip()
        first, last, actual_rows, loops = actual_match.groups()
        actual = {
            "time_first_ms": _number(first),
            "time_last_ms": _number(last),
            "actual_rows": _number(actual_rows),
            "loops": _number(loops),
        }
    else:
        actual = {
            "time_first_ms": None,
            "time_last_ms": None,
            "actual_rows": None,
            "loops": None,
        }

    cost_match = _COST.search(remaining)
    if cost_match:
        remaining = _COST.sub("", remaining).rstrip()
        cost, estimated_rows = cost_match.groups()
        estimated_cost = _number(cost.rsplit("..", 1)[-1])
        estimated_rows = _number(estimated_rows)
    else:
        estimated_cost = None
        estimated_rows = None

    stats: dict[str, Any] = {
        "estimated_rows": estimated_rows,
        "estimated_cost": estimated_cost,
        **actual,
        "never_executed": never_executed,
    }
    if never_executed:
        stats.update(
            actual_rows=0,
            loops=0,
            time_first_ms=0,
            time_last_ms=0,
        )
    return remaining.rstrip(), stats


def _operator_and_details(node_text: str) -> tuple[str, str]:
    for operator in _OPERATORS:
        if node_text.startswith(operator):
            details = node_text[len(operator) :].lstrip(": ")
            return operator, details

    split_at = len(node_text)
    for marker in (":", " on ", " using ", "("):
        found = node_text.find(marker)
        if found >= 0:
            split_at = min(split_at, found)
    operator = node_text[:split_at].strip()
    details = node_text[split_at:].lstrip(": ")
    return operator, details


def parse_explain_analyze(raw_text: str) -> dict[str, Any]:
    """Parse textual EXPLAIN ANALYZE output into one normalized PlanNode tree."""
    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")

    lines = raw_text.replace("\r\n", "\n").split("\n")
    root: dict[str, Any] | None = None
    stack: list[dict[str, Any]] = []
    previous_depth = 0
    node_number = 0

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        match = _NODE_LINE.match(line)
        if not match:
            raise ParseError("Expected an indented '-> ' node line", line_no, line)

        indent, body = match.groups()
        if len(indent) % 4:
            raise ParseError("Indentation must be a multiple of four spaces", line_no, line)
        depth = len(indent) // 4
        if root is None and depth != 0:
            raise ParseError("The first node must be the root at depth zero", line_no, line)
        if root is not None and depth == 0:
            raise ParseError("A plan must contain exactly one root", line_no, line)
        if root is not None and depth > previous_depth + 1:
            raise ParseError("Node depth may increase by at most one", line_no, line)
        if depth > len(stack):
            raise ParseError("Node has no parent at the requested depth", line_no, line)

        node_text, stats = _split_stats(body)
        operator, details = _operator_and_details(node_text)
        node_number += 1
        node: dict[str, Any] = {
            "id": f"node_{node_number}",
            "operator": operator,
            "details": details,
            "depth": depth,
            **stats,
            "rows_total": None,
            "time_total_ms": None,
            "self_time_ms": 0,
            "self_time_pct": 0,
            "warnings": [],
            "children": [],
        }

        if depth == 0:
            root = node
            stack = [node]
        else:
            parent = stack[depth - 1]
            parent["children"].append(node)
            stack = stack[:depth]
            stack.append(node)
        previous_depth = depth

    if root is None:
        raise ParseError("Plan is empty", 0, "")

    def set_totals(node: dict[str, Any]) -> float | int:
        for child in node["children"]:
            set_totals(child)

        if node["never_executed"]:
            node["rows_total"] = 0
            node["time_total_ms"] = 0
        else:
            if node["actual_rows"] is not None and node["loops"] is not None:
                node["rows_total"] = node["actual_rows"] * node["loops"]
            if node["time_last_ms"] is not None and node["loops"] is not None:
                node["time_total_ms"] = node["time_last_ms"] * node["loops"]

        child_time = sum(_accounted_time(child) for child in node["children"])
        total = node["time_total_ms"]
        if total is None:
            # Untimed wrapper nodes (for example MySQL's Hash node) contribute
            # no self time; their timed descendants remain attributed upstream.
            node["self_time_ms"] = 0
        else:
            node["self_time_ms"] = max(0, total - child_time)
        return _accounted_time(node)

    def _accounted_time(node: dict[str, Any]) -> float | int:
        total = node["time_total_ms"]
        if total is not None:
            return total
        return sum(_accounted_time(child) for child in node["children"])

    set_totals(root)
    root_time = root["time_total_ms"]

    def set_percentages(node: dict[str, Any]) -> None:
        if root_time is not None and root_time > 0:
            node["self_time_pct"] = round(node["self_time_ms"] / root_time * 100, 2)
        else:
            node["self_time_pct"] = 0
        for child in node["children"]:
            set_percentages(child)

    set_percentages(root)
    return root
