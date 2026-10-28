"""Analyze normalized plans and emit evidence-backed student explanations."""

from __future__ import annotations

import math
from typing import Any


Warning = dict[str, Any]

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
_WARNING_CLASSIFICATION = {
    "FULL_SCAN": "observation",
    "LARGE_SORT": "observation",
    "TEMP_TABLE": "observation",
    "DEAD_END": "observation",
    "NEVER_EXECUTED": "observation",
    "INNER_SCAN_IN_LOOP": "investigate",
    "FILTER_DISCARDS_MOST": "investigate",
    "ESTIMATE_MISMATCH": "investigate",
    "HOTSPOT": "investigate",
}


def _walk(node: dict[str, Any], ancestors: tuple[dict[str, Any], ...] = ()):
    yield node, ancestors
    for child in node.get("children", []):
        yield from _walk(child, ancestors + (node,))


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _format_rows(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "unavailable"
    if isinstance(number, float) and not number.is_integer():
        return f"{number:,.6f}".rstrip("0").rstrip(".")
    return f"{int(number):,}"


def _rows_label(value: Any) -> str:
    count = _format_rows(value)
    return f"{count} row" if _number(value) == 1 else f"{count} rows"


def _loops_label(value: Any) -> str:
    count = _format_rows(value)
    return f"{count} loop" if _number(value) == 1 else f"{count} loops"


def _loop_total_label(rows_per_loop: Any, loops: Any) -> str:
    per_loop = _number(rows_per_loop)
    loop_count = _number(loops)
    if per_loop is None or loop_count is None:
        return "total rows unavailable"
    total = per_loop * loop_count
    approximation = "approximately " if isinstance(per_loop, float) and not per_loop.is_integer() else ""
    return f"{approximation}{_rows_label(total)} across loops"


def _format_ms(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "unavailable"
    if number == 0:
        return "0 ms"
    formatted = f"{number:,.4g}" if number >= 1 else f"{number:.3g}"
    return f"{formatted} ms"


def _warning(
    code: str,
    severity: str,
    node: dict[str, Any],
    title: str,
    message: str,
    evidence: str,
    suggestion: str | None,
) -> Warning:
    return {
        "code": code,
        "classification": _WARNING_CLASSIFICATION.get(code, "investigate"),
        "severity": severity,
        "node_id": node["id"],
        "title": title,
        "message": message,
        "evidence": evidence,
        "suggestion": suggestion,
    }


def _id_number(node_id: str) -> int:
    try:
        return int(node_id.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return 0


def _operator_description(node: dict[str, Any]) -> str:
    operator = str(node.get("operator") or "Plan step")
    details = str(node.get("details") or "").strip()
    return f"{operator} {details}".strip()


def _work_severity(rows: Any, elapsed_ms: Any) -> str:
    row_count = _number(rows)
    elapsed = _number(elapsed_ms)
    if elapsed is not None and elapsed >= 2000:
        return "high"
    if elapsed is not None and elapsed >= 1000:
        return "medium"
    if elapsed is not None and elapsed >= 250 and row_count is not None and row_count >= 10000:
        return "medium"
    if row_count is not None and row_count >= 100000 and (elapsed is None or elapsed >= 100):
        return "medium"
    if (row_count is not None and row_count >= 10000) or (elapsed is not None and elapsed >= 25):
        return "low"
    return "info"


def _selective_filter(node: dict[str, Any]) -> bool:
    if node.get("operator") != "Filter" or node.get("never_executed") or not node.get("children"):
        return False
    input_rows = _number(node["children"][0].get("rows_total"))
    output_rows = _number(node.get("rows_total"))
    return bool(
        input_rows is not None
        and output_rows is not None
        and input_rows >= 1000
        and output_rows <= input_rows * 0.1
    )


def _has_selective_filter_ancestor(ancestors: tuple[dict[str, Any], ...]) -> bool:
    return any(_selective_filter(parent) for parent in ancestors)


def _real_table_scan(node: dict[str, Any]) -> bool:
    if node.get("operator") != "Table scan":
        return False
    details = str(node.get("details") or "").casefold()
    internal_sources = ("<temporary", "<subquery", "<derived", "<cte")
    return not any(marker in details for marker in internal_sources)


def _scan_warning_evidence(node: dict[str, Any], loops: Any, rows: Any) -> str:
    return (
        f"{_operator_description(node)} · {_rows_label(node.get('actual_rows'))} per loop on average · "
        f"{_loops_label(loops)} · {_loop_total_label(node.get('actual_rows'), loops)} · "
        f"self time {_format_ms(node.get('self_time_ms'))}."
    )


def _estimate_warning(node: dict[str, Any]) -> Warning | None:
    estimated = _number(node.get("estimated_rows"))
    actual = _number(node.get("actual_rows"))
    loops = _number(node.get("loops"))
    total_actual = _number(node.get("rows_total"))
    if (
        node.get("never_executed")
        or estimated is None
        or actual is None
        or loops is None
        or loops <= 0
        or total_actual is None
    ):
        return None

    # A parent that also returned zero because an input was empty repeats the
    # same finding; report the operator where rows first stopped instead.
    if actual == 0 and any(
        child.get("never_executed") or _number(child.get("rows_total")) == 0
        for child in node.get("children", [])
    ):
        return None

    difference = abs(estimated - actual)
    smaller = min(estimated, actual)
    ratio = max(estimated, actual) / smaller if smaller > 0 else math.inf if max(estimated, actual) > 0 else 1
    if ratio < 10 or difference < 100:
        return None

    # A selective Filter already explains this row-flow finding more directly.
    if _selective_filter(node):
        return None

    greater = actual > estimated
    title = "MySQL returned far more rows than expected" if greater else "MySQL expected far more rows"
    if greater:
        message = (
            "This step produced many more rows per loop than MySQL predicted. Large estimate errors can affect "
            "join order and access choices, though this difference alone does not prove the query is slow."
        )
    else:
        message = (
            "This step produced many fewer rows per loop than MySQL predicted. That can affect join order and "
            "access choices, but the difference alone does not prove the plan is inefficient."
        )

    elapsed = _number(node.get("self_time_ms"))
    expected_total = estimated * loops
    largest_rows = max(expected_total, total_actual)
    severity = "high" if elapsed is not None and elapsed >= 2000 else (
        "medium"
        if (elapsed is not None and elapsed >= 250)
        or largest_rows >= 100000
        or (ratio >= 100 and max(estimated, actual) >= 100)
        else "low"
    )
    description = _operator_description(node)
    evidence = (
        f"{description} · estimate {_rows_label(estimated)} per loop · "
        f"actual average {_rows_label(actual)} per loop · {_loops_label(loops)} · "
        f"{_loop_total_label(actual, loops)}."
    )
    return _warning(
        "ESTIMATE_MISMATCH",
        severity,
        node,
        title,
        message,
        evidence,
        "If this plan is slow, inspect statistics and value distribution for the predicates or join keys shown here; the estimate alone does not prove an index is missing.",
    )


def _dead_end_warnings(tree: dict[str, Any], nodes: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]], warnings: list[Warning]) -> list[Warning]:
    boundaries = []
    for node, _ancestors in nodes:
        if node.get("never_executed") or _number(node.get("loops")) in (None, 0):
            continue
        if _number(node.get("actual_rows")) != 0 or _number(node.get("rows_total")) != 0:
            continue
        if any((_number(child.get("rows_total")) or 0) > 0 for child in node.get("children", [])):
            boundaries.append(node)

    if not boundaries and _number(tree.get("actual_rows")) == 0 and _number(tree.get("loops")) not in (None, 0):
        boundaries = [tree]

    warning_nodes = {warning["node_id"] for warning in warnings if warning["code"] in {"FILTER_DISCARDS_MOST", "ESTIMATE_MISMATCH"}}
    result = []
    for node in boundaries:
        if node["id"] in warning_nodes:
            continue
        child_rows = sum(_number(child.get("rows_total")) or 0 for child in node.get("children", []))
        evidence = f"{_operator_description(node)} · 0 rows out"
        if child_rows > 0:
            evidence += f" · {_rows_label(child_rows)} from its inputs"
        evidence += f" · {_loops_label(node.get('loops'))}."
        result.append(
            _warning(
                "DEAD_END",
                "info",
                node,
                "No rows came out of this step",
                "This operation produced no output rows. If the query was expected to return nothing, no action is needed.",
                evidence,
                "If you expected results, inspect this step's condition and the rows it received.",
            )
        )
        result[-1]["type"] = "DEAD_END"
    return result


def annotate_dead_end_nodes(tree: dict[str, Any]) -> dict[str, Any]:
    """Mark executed zero-output nodes for the existing plan inspector UI."""
    for node, _ancestors in _walk(tree):
        if (
            not node.get("never_executed")
            and _number(node.get("loops")) not in (None, 0)
            and _number(node.get("actual_rows")) == 0
        ):
            node["dead_end"] = True
    return tree


def analyze_rules(tree: dict[str, Any]) -> list[Warning]:
    """Return contextual warnings without changing the execution plan."""
    nodes = list(_walk(tree))
    warnings: list[Warning] = []

    for node, ancestors in nodes:
        operator = str(node.get("operator") or "")
        details = str(node.get("details") or "").strip()
        rows_total = _number(node.get("rows_total"))
        loops = _number(node.get("loops"))
        self_time = _number(node.get("self_time_ms"))

        is_real_scan = _real_table_scan(node)
        if is_real_scan and loops is not None and loops > 1 and rows_total is not None:
            if rows_total >= 1000 or (self_time is not None and self_time >= 25):
                warnings.append(
                    _warning(
                        "INNER_SCAN_IN_LOOP",
                        _work_severity(rows_total, self_time),
                        node,
                        "MySQL repeated a table scan",
                        "MySQL read this table more than once as part of the plan. Repeated reads can add work, especially when each pass processes many rows.",
                        _scan_warning_evidence(node, loops, rows_total),
                        "If this query is slow, check the join condition and whether an existing index could support repeated lookups; a scan can still be reasonable for small inputs.",
                    )
                )
        elif (
            is_real_scan
            and loops == 1
            and rows_total is not None
            and rows_total > 1000
            and not _has_selective_filter_ancestor(ancestors)
        ):
            severity = _work_severity(rows_total, self_time)
            scan_target = details.removeprefix("on ").strip() or "the table"
            message = (
                "A full table scan reads rows without using an index to find specific records. It can be efficient when the query needs "
                "many rows; it is worth investigating only when the query is slow or should return a small subset."
            )
            warnings.append(
                _warning(
                    "FULL_SCAN",
                    severity,
                    node,
                    "MySQL scanned many rows",
                    message,
                    f"Table scan on {scan_target} · {_rows_label(rows_total)} read · 1 loop · self time {_format_ms(self_time)}.",
                    "If the query is slow or should return few rows, inspect its filters and available indexes; this plan alone cannot show whether an index would be faster.",
                )
            )

        if operator == "Sort" and node.get("children"):
            bounded_top_n = "limit input to" in details.casefold()
            under_limit = any(parent.get("operator") == "Limit" for parent in ancestors)
            input_rows = _number(node["children"][0].get("rows_total"))
            if input_rows is not None and input_rows > 1000 and not (bounded_top_n or under_limit):
                description = _operator_description(node)
                warnings.append(
                    _warning(
                        "LARGE_SORT",
                        _work_severity(input_rows, self_time),
                        node,
                        "MySQL sorted many rows",
                        "MySQL used a Sort step (also called Filesort) to put these rows in order. Sorting can be expected; the name does not mean data was written to disk.",
                        f"{description} · {_rows_label(input_rows)} input rows · self time {_format_ms(self_time)} · {_loops_label(loops)}.",
                        "If sorting adds noticeable latency, check whether an existing index can provide this order; do not add one solely because a Sort step appears.",
                    )
                )

        estimate_warning = _estimate_warning(node)
        if estimate_warning is not None:
            warnings.append(estimate_warning)

        if operator == "Filter" and not node.get("never_executed") and node.get("children"):
            child = node["children"][0]
            input_rows = _number(child.get("rows_total"))
            output_rows = rows_total
            filter_loops = _number(node.get("loops"))
            if (
                input_rows is not None
                and output_rows is not None
                and filter_loops is not None
                and filter_loops > 0
                and input_rows >= 1000
                and output_rows <= input_rows * 0.1
            ):
                kept_pct = output_rows / input_rows * 100 if input_rows else 0
                child_time = _number(child.get("time_total_ms"))
                severity = _work_severity(input_rows, child_time)
                predicate = details.removeprefix(":").strip()
                evidence = (
                    f"Filter {predicate} · kept {_format_rows(output_rows)} of {_format_rows(input_rows)} input rows "
                    f"({kept_pct:.2f}% kept) · {_loops_label(filter_loops)} · "
                    f"input time {_format_ms(child_time)}."
                )
                warnings.append(
                    _warning(
                        "FILTER_DISCARDS_MOST",
                        severity,
                        node,
                        "This filter kept only a few rows",
                        "The Filter rejected most of its input after receiving those rows. That may be a useful selectivity for the query, but could be avoidable work if the query is slow.",
                        evidence,
                        "If this query is slow, check whether an existing index can apply this filter before reading the input; the plan does not list all available indexes.",
                    )
                )

        if "temporary table" in operator.casefold():
            evidence = (
                f"{_operator_description(node)} · output {_loop_total_label(node.get('actual_rows'), loops)} · "
                f"self time {_format_ms(self_time)} · {_loops_label(loops)}."
            )
            warnings.append(
                _warning(
                    "TEMP_TABLE",
                    "info",
                    node,
                    "MySQL used an internal temporary table",
                    "MySQL used temporary storage for this aggregation. This is common with GROUP BY and does not by itself indicate a performance problem; the plan does not say whether data spilled to disk.",
                    evidence,
                    "No change is needed unless this step contributes noticeable query time. If it does, review the grouping keys and measured time.",
                )
            )

        if node.get("never_executed"):
            evidence = f"{_operator_description(node)} · marked never executed · 0 loops · no measured output or runtime."
            warnings.append(
                _warning(
                    "NEVER_EXECUTED",
                    "info",
                    node,
                    "MySQL skipped this operation",
                    "This step did not run, so it has no actual row count or runtime. An earlier operation may have left it with no input; check the plan if you expected it to run.",
                    evidence,
                    "If you expected this step to run, follow its input branch to the earlier step that stopped producing rows.",
                )
            )

    valid_total = _number(tree.get("time_total_ms"))
    timed_nodes = [
        node for node, _ancestors in nodes
        if _number(node.get("self_time_ms")) is not None
    ]
    if valid_total is not None and valid_total > 0 and timed_nodes:
        hotspot = max(timed_nodes, key=lambda item: _number(item.get("self_time_ms")) or 0)
        self_time = _number(hotspot.get("self_time_ms")) or 0
        share = self_time / valid_total * 100
        if self_time >= 50 and share >= 40:
            severity = "high" if self_time >= 2000 else "medium" if self_time >= 500 else "low" if self_time >= 100 else "info"
            description = _operator_description(hotspot)
            warnings.append(
                _warning(
                    "HOTSPOT",
                    severity,
                    hotspot,
                    "Most measured time was spent in this step",
                    f"MySQL spent {_format_ms(self_time)} on this step's own work, excluding child steps. This was the largest measured share of the plan, but that alone does not mean the query is slow.",
                    f"{description} · self time {_format_ms(self_time)} · {share:.2f}% of {_format_ms(valid_total)} plan time · {_loops_label(hotspot.get('loops'))}.",
                    "If the overall query is too slow, investigate this operation and its row volume; the timing shows where time went, not the underlying cause.",
                )
            )

    warnings.extend(_dead_end_warnings(tree, nodes, warnings))
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
