import copy
from pathlib import Path

import pytest

from core.parser import parse_explain_analyze
from core.rules import analyze_rules, annotate_dead_end_nodes, annotate_tree


ROOT = Path(__file__).resolve().parents[1]
FALLBACKS = ROOT / "demo" / "plans_fallback"
GOLDEN_CODES = {
    "01_full_scan": {"FULL_SCAN"},
    "02_index_lookup": set(),
    "03_join_no_index": {"FULL_SCAN", "ESTIMATE_MISMATCH"},
    "04_group_by": set(),
    "05_sort_limit": {"FULL_SCAN"},
    "06_subquery": {"FILTER_DISCARDS_MOST"},
    "07_zero_rows": {"FILTER_DISCARDS_MOST", "NEVER_EXECUTED"},
    "08_full_pipeline": {"FULL_SCAN", "TEMP_TABLE"},
    "09_slow_showcase": {"FULL_SCAN", "LARGE_SORT"},
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


def test_small_full_table_scan_is_not_reported_as_a_problem():
    tree = parse_explain_analyze(
        "-> Table scan on lookup (cost=1 rows=40) (actual time=0.01..0.02 rows=40 loops=1)"
    )
    assert analyze_rules(tree) == []


def test_large_full_scan_uses_measured_rows_and_conditional_guidance():
    tree = parse_explain_analyze(
        "-> Table scan on orders (cost=20 rows=12000) (actual time=0.1..120 rows=12000 loops=1)"
    )
    warning = next(item for item in analyze_rules(tree) if item["code"] == "FULL_SCAN")

    assert warning["node_id"] == tree["id"]
    assert warning["severity"] == "low"
    assert "full table scan" in warning["message"].lower()
    assert "12,000 rows average rows per loop" not in warning["evidence"]
    assert "12,000 rows read" in warning["evidence"]
    assert "if the query is slow" in warning["suggestion"].lower()
    assert "guarantee" not in warning["suggestion"].lower()


def test_small_repeated_scan_does_not_create_an_alarm():
    plan = (ROOT / "tests" / "fixtures" / "loop_scan.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    scan = next(node for node in _nodes(tree) if node["operator"] == "Table scan" and "orders" in node["details"])
    assert scan["rows_total"] == 60
    assert not any(
        item["code"] == "INNER_SCAN_IN_LOOP" and item["node_id"] == scan["id"]
        for item in analyze_rules(tree)
    )


def test_significant_repeated_scan_reports_average_loops_and_approximate_total():
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=20 rows=1200) (actual time=0.1..80 rows=1200 loops=1)\n"
        "    -> Table scan on customers (cost=1 rows=3) (actual time=0.01..0.02 rows=3 loops=1)\n"
        "    -> Table scan on orders (cost=10 rows=400) (actual time=0.05..20 rows=400 loops=3)"
    )
    scan = next(node for node in _nodes(tree) if node["details"] == "on orders")
    warning = next(
        item for item in analyze_rules(tree)
        if item["code"] == "INNER_SCAN_IN_LOOP" and item["node_id"] == scan["id"]
    )

    assert warning["severity"] == "low"
    assert "400 rows per loop on average" in warning["evidence"]
    assert "3 loops" in warning["evidence"]
    assert "1,200 rows across loops" in warning["evidence"]
    assert "small inputs" in warning["suggestion"]


def test_index_lookup_is_not_called_a_missing_index_without_evidence():
    tree = parse_explain_analyze(
        "-> Index range scan on orders using ix_date (date between 1 and 2) (cost=3 rows=90) (actual time=0.01..0.5 rows=90 loops=1)"
    )
    warnings = analyze_rules(tree)
    assert warnings == []


@pytest.mark.parametrize("source", ["<temporary>", "<derived2>", "<subquery2>"])
def test_internal_table_scans_are_not_treated_as_base_table_scans(source):
    tree = parse_explain_analyze(
        f"-> Table scan on {source} (cost=20 rows=12000) "
        "(actual time=0.1..120 rows=12000 loops=1)"
    )
    warnings = analyze_rules(tree)
    assert not any(item["code"] in {"FULL_SCAN", "INNER_SCAN_IN_LOOP"} for item in warnings)


def test_temporary_table_notice_is_informational_and_does_not_imply_disk_spill():
    plan = (FALLBACKS / "08_full_pipeline.txt").read_text(encoding="utf-8")
    warning = next(item for item in analyze_rules(parse_explain_analyze(plan)) if item["code"] == "TEMP_TABLE")

    assert warning["severity"] == "info"
    assert warning["classification"] == "observation"
    assert "temporary table" in warning["title"].lower()
    assert "common with GROUP BY" in warning["message"]
    assert "does not say whether data spilled to disk" in warning["message"]
    assert "no change is needed unless" in warning["suggestion"].lower()


def test_filesort_explains_sorting_without_claiming_disk_use():
    tree = parse_explain_analyze(
        "-> Sort: orders.created_at (cost=30 rows=5000) (actual time=1..80 rows=5000 loops=1)\n"
        "    -> Table scan on orders (cost=20 rows=5000) (actual time=0.1..30 rows=5000 loops=1)"
    )
    warning = next(item for item in analyze_rules(tree) if item["code"] == "LARGE_SORT")

    assert warning["node_id"] == tree["id"]
    assert "Filesort" in warning["message"]
    assert "does not mean data was written to disk" in warning["message"]
    assert "5,000 rows input" in warning["evidence"]
    assert "do not add one solely" in warning["suggestion"]


def test_sort_with_limit_input_is_not_warned():
    plan = (FALLBACKS / "05_sort_limit.txt").read_text(encoding="utf-8")
    warnings = analyze_rules(parse_explain_analyze(plan))
    assert "LARGE_SORT" not in {item["code"] for item in warnings}


def test_estimate_comparison_uses_average_rows_per_loop_not_total_rows():
    plan = (ROOT / "tests" / "fixtures" / "plans" / "06_subquery.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    lookup = next(node for node in _nodes(tree) if node["operator"] == "Single-row index lookup")
    warnings = analyze_rules(tree)

    assert lookup["estimated_rows"] == 1
    assert lookup["actual_rows"] == 0.33
    assert lookup["loops"] == 500
    assert lookup["rows_total"] == pytest.approx(165)
    assert not any(item["code"] == "ESTIMATE_MISMATCH" and item["node_id"] == lookup["id"] for item in warnings)


def test_estimate_mismatch_evidence_labels_average_and_approximate_total():
    tree = parse_explain_analyze(
        "-> Index lookup on t using i (id=1) (cost=1 rows=1000) (actual time=0.1..0.2 rows=1 loops=500)"
    )
    warning = next(item for item in analyze_rules(tree) if item["code"] == "ESTIMATE_MISMATCH")

    assert warning["severity"] == "medium"
    assert "1,000 rows per loop" in warning["evidence"]
    assert "actual average 1 row per loop" in warning["evidence"]
    assert "500 rows across loops" in warning["evidence"]
    assert "per pass" not in warning["message"]


def test_nested_loop_and_filter_mismatch_evidence_uses_singular_loop_count():
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=100 rows=219) (actual time=0.1..20 rows=2247 loops=1)\n"
        "    -> Filter: (orders.status = 'open') (cost=100 rows=219) (actual time=0.1..10 rows=2247 loops=1)\n"
        "        -> Table scan on orders (cost=100 rows=5000) (actual time=0.1..8 rows=5000 loops=1)\n"
        "    -> Single-row index lookup on customers using PRIMARY (id=orders.customer_id) "
        "(cost=0.25 rows=1) (actual time=0.01..0.02 rows=1 loops=2247)"
    )
    mismatches = [item for item in analyze_rules(tree) if item["code"] == "ESTIMATE_MISMATCH"]

    assert {item["node_id"] for item in mismatches} == {
        tree["id"],
        tree["children"][0]["id"],
    }
    for warning in mismatches:
        assert "219 rows per loop" in warning["evidence"]
        assert "actual average 2,247 rows per loop" in warning["evidence"]
        assert "1 loop" in warning["evidence"]
        assert "1 loops" not in warning["evidence"]


def test_fractional_rows_and_scientific_notation_produce_contextual_mismatch():
    tree = parse_explain_analyze(
        "-> Inner hash join (cost=1e-2 rows=6e-1) (actual time=1e-1..2e-1 rows=1.373e+2 loops=3)"
    )
    warning = next(item for item in analyze_rules(tree) if item["code"] == "ESTIMATE_MISMATCH")

    assert tree["estimated_rows"] == 0.6
    assert tree["actual_rows"] == pytest.approx(137.3)
    assert warning["severity"] == "medium"
    assert "far more rows" in warning["title"]
    assert "0.6 rows per loop" in warning["evidence"]
    assert "actual average 137.3 rows per loop" in warning["evidence"]
    assert "approximately 411.9 rows across loops" in warning["evidence"]
    assert "actual total rows unavailable" not in warning["evidence"]


@pytest.mark.parametrize(
    "estimated,actual",
    [(100, 150), (1000, 1500), (1000, 150)],
)
def test_small_or_subthreshold_estimate_difference_is_not_warned(estimated, actual):
    tree = parse_explain_analyze(
        f"-> Index lookup on t using i (id=1) (cost=1 rows={estimated}) "
        f"(actual time=0.1..0.2 rows={actual} loops=1)"
    )
    assert "ESTIMATE_MISMATCH" not in {item["code"] for item in analyze_rules(tree)}


def test_limit_estimate_mismatch_is_reported_when_its_input_has_rows():
    tree = parse_explain_analyze(
        "-> Limit: 10000 row(s) (cost=1 rows=10000) (actual time=0.1..0.2 rows=1000 loops=1)\n"
        "    -> Table scan on t (cost=1 rows=1000) (actual time=0.1..0.2 rows=1000 loops=1)"
    )
    warning = next(
        item for item in analyze_rules(tree)
        if item["code"] == "ESTIMATE_MISMATCH" and item["node_id"] == tree["id"]
    )
    assert "10,000 rows per loop" in warning["evidence"]
    assert "1,000 rows per loop" in warning["evidence"]


def test_filter_warning_suppresses_duplicate_scan_and_estimate_findings():
    tree = parse_explain_analyze(
        "-> Filter: (orders.status = 'open') (cost=10 rows=10) (actual time=0.1..5 rows=10 loops=1)\n"
        "    -> Table scan on orders (cost=10 rows=5000) (actual time=0.1..20 rows=5000 loops=1)"
    )
    warnings = analyze_rules(tree)
    filter_warning = next(item for item in warnings if item["code"] == "FILTER_DISCARDS_MOST")
    scan = tree["children"][0]

    assert not any(item["code"] == "FULL_SCAN" and item["node_id"] == scan["id"] for item in warnings)
    assert not any(item["code"] == "ESTIMATE_MISMATCH" and item["node_id"] == tree["id"] for item in warnings)
    assert "orders.status = 'open'" in filter_warning["evidence"]
    assert "kept 10 of 5,000 input rows" in filter_warning["evidence"]
    assert "query is slow" in filter_warning["message"].lower()


def test_zero_rows_boundary_has_one_informational_node_linked_notice():
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=2 rows=1) (actual time=0.1..0.2 rows=0 loops=1)\n"
        "    -> Table scan on customers (cost=1 rows=500) (actual time=0.01..0.2 rows=500 loops=1)"
    )
    warnings = analyze_rules(tree)
    dead_ends = [item for item in warnings if item["code"] == "DEAD_END"]

    assert len(dead_ends) == 1
    assert dead_ends[0]["node_id"] == tree["id"]
    assert dead_ends[0]["type"] == "DEAD_END"
    assert "0 rows out" in dead_ends[0]["evidence"]


def test_zero_rows_and_never_executed_nodes_do_not_duplicate_findings():
    plan = (FALLBACKS / "07_zero_rows.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    warnings = analyze_rules(tree)
    never = next(node for node in _nodes(tree) if node["never_executed"])

    assert any(item["code"] == "NEVER_EXECUTED" and item["node_id"] == never["id"] for item in warnings)
    assert not any(item["code"] == "ESTIMATE_MISMATCH" and item["node_id"] == never["id"] for item in warnings)
    assert not any(item["code"] == "DEAD_END" and item["node_id"] == never["id"] for item in warnings)


def test_hotspot_uses_own_time_not_inclusive_child_time():
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=1 rows=1) (actual time=0.1..1000 rows=1 loops=1)\n"
        "    -> Filter: (t.id > 0) (cost=1 rows=1) (actual time=0.1..800 rows=1 loops=1)\n"
        "        -> Index lookup on t using i (id=1) (cost=1 rows=1) (actual time=0.1..700 rows=1 loops=1)"
    )
    warnings = analyze_rules(tree)
    hotspot = next(item for item in warnings if item["code"] == "HOTSPOT")
    lookup = tree["children"][0]["children"][0]

    assert lookup["time_total_ms"] == 700
    assert lookup["self_time_ms"] == 700
    assert hotspot["node_id"] == lookup["id"]
    assert hotspot["classification"] == "investigate"
    assert "self time 700 ms" in hotspot["evidence"]
    assert "70.00% of 1,000 ms plan time" in hotspot["evidence"]
    assert "excluding child steps" in hotspot["message"]
    assert "does not mean the query is slow" in hotspot["message"]


def test_hotspot_can_be_the_root_when_its_own_work_is_largest():
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=1 rows=1) (actual time=0.1..1000 rows=1 loops=1)\n"
        "    -> Index lookup on t using i (id=1) (cost=1 rows=1) (actual time=0.1..100 rows=1 loops=1)"
    )
    hotspot = next(item for item in analyze_rules(tree) if item["code"] == "HOTSPOT")

    assert hotspot["node_id"] == tree["id"]
    assert "self time 900 ms" in hotspot["evidence"]
    assert "90.00%" in hotspot["evidence"]


def test_high_severity_requires_a_long_measured_self_time():
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=1 rows=1) (actual time=0.1..2600 rows=1 loops=1)\n"
        "    -> Index lookup on t using i (id=1) (cost=1 rows=1) (actual time=0.1..100 rows=1 loops=1)"
    )
    hotspot = next(item for item in analyze_rules(tree) if item["code"] == "HOTSPOT")
    assert hotspot["severity"] == "high"


def test_missing_statistics_do_not_create_data_dependent_warnings():
    tree = parse_explain_analyze("-> Table scan on unknown_stats (cost=1 rows=5000)")
    assert tree["actual_rows"] is None
    assert tree["loops"] is None
    assert analyze_rules(tree) == []


def test_a_plan_without_actionable_findings_has_no_warning_cards():
    tree = parse_explain_analyze(
        "-> Index range scan on t using ix_id (id between 1 and 5) (cost=1 rows=5) (actual time=0.01..0.02 rows=5 loops=1)"
    )
    assert analyze_rules(tree) == []


def test_dead_end_annotation_skips_never_executed_nodes():
    tree = parse_explain_analyze((FALLBACKS / "07_zero_rows.txt").read_text(encoding="utf-8"))
    never = next(node for node in _nodes(tree) if node["never_executed"])
    annotate_dead_end_nodes(tree)
    assert not never.get("dead_end", False)


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


def test_every_warning_has_evidence_operator_node_and_practical_guidance():
    for plan_path in FALLBACKS.glob("*.txt"):
        warnings = analyze_rules(parse_explain_analyze(plan_path.read_text(encoding="utf-8")))
        for warning in warnings:
            allowed_fields = {"code", "classification", "severity", "node_id", "title", "message", "evidence", "suggestion"}
            if warning["code"] == "DEAD_END":
                allowed_fields.add("type")
            assert set(warning) == allowed_fields
            assert warning["title"]
            assert warning["classification"] in {"observation", "investigate", "action"}
            assert warning["message"].endswith(".")
            assert warning["evidence"]
            assert warning["suggestion"]
            assert warning["node_id"].startswith("node_")
            assert "".join(character for character in warning["evidence"] if character.isalpha())


def test_observations_and_investigation_findings_have_distinct_categories():
    scan = parse_explain_analyze(
        "-> Table scan on orders (cost=20 rows=12000) (actual time=0.1..120 rows=12000 loops=1)"
    )
    mismatch = parse_explain_analyze(
        "-> Index lookup on t using i (id=1) (cost=1 rows=1000) (actual time=0.1..0.2 rows=1 loops=1)"
    )
    scan_warning = next(item for item in analyze_rules(scan) if item["code"] == "FULL_SCAN")
    mismatch_warning = next(item for item in analyze_rules(mismatch) if item["code"] == "ESTIMATE_MISMATCH")

    assert scan_warning["classification"] == "observation"
    assert mismatch_warning["classification"] == "investigate"
