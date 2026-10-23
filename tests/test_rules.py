import copy
from pathlib import Path

import pytest

from core.parser import parse_explain_analyze
from core.rules import analyze_rules, annotate_tree


ROOT = Path(__file__).resolve().parents[1]
FALLBACKS = ROOT / "demo" / "plans_fallback"
GOLDEN_CODES = {
    "01_full_scan": {"FULL_SCAN", "HOTSPOT"},
    "02_index_lookup": set(),
    "03_join_no_index": {"FULL_SCAN", "ESTIMATE_MISMATCH", "HOTSPOT"},
    "04_group_by": {"HOTSPOT"},
    "05_sort_limit": {"FULL_SCAN", "HOTSPOT"},
    "06_subquery": {"FULL_SCAN", "FILTER_DISCARDS_MOST", "HOTSPOT"},
    "07_zero_rows": {"FULL_SCAN", "FILTER_DISCARDS_MOST", "HOTSPOT", "NEVER_EXECUTED"},
    "08_full_pipeline": {"FULL_SCAN", "TEMP_TABLE", "HOTSPOT"},
    "09_slow_showcase": {"FULL_SCAN", "LARGE_SORT", "HOTSPOT"},
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _nodes(tree):
    yield tree
    for child in tree["children"]:
        yield from _nodes(child)


@pytest.mark.parametrize("fixture_id,expected_codes", GOLDEN_CODES.items())
def test_golden_warning_codes(fixture_id, expected_codes):
    plan = (FALLBACKS / f"{fixture_id}.txt").read_text(encoding="utf-8")
    warnings = analyze_rules(parse_explain_analyze(plan))
    assert {warning["code"] for warning in warnings} == expected_codes


def test_loop_scan_uses_inner_scan_warning_instead_of_full_scan():
    plan = (ROOT / "tests" / "fixtures" / "loop_scan.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    warnings = analyze_rules(tree)
    scan = next(node for node in _nodes(tree) if node["operator"] == "Table scan" and "orders" in node["details"])
    scan_codes = {item["code"] for item in warnings if item["node_id"] == scan["id"]}
    assert "INNER_SCAN_IN_LOOP" in scan_codes
    assert "FULL_SCAN" not in scan_codes


def test_zero_rows_plan_reports_never_executed_without_estimate_mismatch_crash():
    plan = (FALLBACKS / "07_zero_rows.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    warnings = analyze_rules(tree)
    never = next(node for node in _nodes(tree) if node["never_executed"])
    assert any(item["code"] == "NEVER_EXECUTED" and item["node_id"] == never["id"] for item in warnings)
    assert all(not (item["code"] == "ESTIMATE_MISMATCH" and item["node_id"] == never["id"]) for item in warnings)


def test_temporary_table_scan_is_not_a_full_scan():
    plan = (FALLBACKS / "08_full_pipeline.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    temporary_scan = next(node for node in _nodes(tree) if node["operator"] == "Table scan" and "<temporary>" in node["details"])
    warnings = analyze_rules(tree)
    assert not any(item["code"] == "FULL_SCAN" and item["node_id"] == temporary_scan["id"] for item in warnings)


def test_sort_limit_plan_does_not_emit_large_sort():
    plan = (FALLBACKS / "05_sort_limit.txt").read_text(encoding="utf-8")
    warnings = analyze_rules(parse_explain_analyze(plan))
    assert "LARGE_SORT" not in {item["code"] for item in warnings}


def test_analyze_rules_is_pure_and_annotate_tree_fills_warning_codes():
    plan = (FALLBACKS / "07_zero_rows.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    before = copy.deepcopy(tree)
    warnings = analyze_rules(tree)
    assert tree == before

    annotate_tree(tree, warnings)
    expected = {}
    for item in warnings:
        expected.setdefault(item["node_id"], []).append(item["code"])
    for node in _nodes(tree):
        assert node["warnings"] == expected.get(node["id"], [])


def test_warnings_are_sorted_by_severity_then_plan_node_id():
    plan = (FALLBACKS / "07_zero_rows.txt").read_text(encoding="utf-8")
    warnings = analyze_rules(parse_explain_analyze(plan))
    sort_keys = [
        (SEVERITY_ORDER[item["severity"]], int(item["node_id"].split("_")[1]))
        for item in warnings
    ]
    assert sort_keys == sorted(sort_keys)


def test_warnings_have_required_fields_and_plain_english_messages():
    plan = (FALLBACKS / "01_full_scan.txt").read_text(encoding="utf-8")
    warnings = analyze_rules(parse_explain_analyze(plan))
    for warning in warnings:
        assert set(warning) == {"code", "severity", "node_id", "title", "message", "suggestion"}
        assert warning["title"] and warning["message"].endswith(".")
