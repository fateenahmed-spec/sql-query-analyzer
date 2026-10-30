from __future__ import annotations

import json
from pathlib import Path

from core.parser import parse_explain_analyze
from db import build_row_flow, get_connection

ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = ROOT / "tests" / "fixtures"


def build_fixture_flows() -> dict[str, list[dict]]:
    queries = json.loads((FIXTURE_ROOT / "queries.json").read_text(encoding="utf-8"))
    if len(queries) < 7:
        raise AssertionError(f"Expected at least 7 fixture queries, found {len(queries)}")

    connection = get_connection()
    flows = {}
    try:
        for fixture_id, query in queries.items():
            plan_path = FIXTURE_ROOT / "plans" / f"{fixture_id}.txt"
            tree = parse_explain_analyze(plan_path.read_text(encoding="utf-8"))
            tree["_flow_query"] = query
            flows[fixture_id] = build_row_flow(tree, connection)
    finally:
        connection.close()
    return flows


def verify_step_chaining(flows: dict[str, list[dict]]) -> None:
    for fixture_id, steps in flows.items():
        by_id = {step["node_id"]: step for step in steps}
        for child in steps:
            if child["parent_id"] is None:
                continue
            parent = by_id[child["parent_id"]]
            branch = next(
                item for item in parent["input_branches"]
                if item["node_id"] == child["node_id"]
            )
            assert branch["output_rids"] == child["output_rids"], (
                f"{fixture_id}: {child['node_id']} output was not kept on its parent branch"
            )


def verify_full_scan_filter(flow: list[dict]) -> None:
    filter_step = next(step for step in flow if step["op"] == "filter")
    assert len(filter_step["input_rids"]) == 10
    for rid in filter_step["input_rids"]:
        row = filter_step["row_data"][rid]["values"]
        expected = float(row["amount"]) > 500
        assert filter_step["row_results"][rid] is expected, (
            f"{rid}: SQL passed={filter_step['row_results'].get(rid)} but amount > 500 is {expected}"
        )
    assert filter_step["evaluable"] is True


def verify_sort_limit(flow: list[dict]) -> None:
    limit_index = next(index for index, step in enumerate(flow) if step["op"] == "limit")
    limit_step = flow[limit_index]
    for step in flow[:limit_index]:
        assert all(step["row_results"].values()), f"{step['label']}: a row failed before Limit"
    assert len(limit_step["output_rids"]) <= 10
    assert len(limit_step["output_rids"]) <= len(limit_step["input_rids"])
    assert limit_step["evaluable"] is True


def verify_zero_rows_skips_join(flow: list[dict]) -> None:
    filter_step = next(step for step in flow if step["op"] == "filter")
    assert len(filter_step["input_rids"]) == 10
    assert filter_step["output_rids"] == []
    assert all(passed is False for passed in filter_step["row_results"].values())
    join_step = next(step for step in flow if step["op"] == "join")
    assert join_step["output_rids"] == []
    assert join_step["exactness"] == "exact_for_sample"
    assert any(step["never_executed"] for step in flow if step["parent_id"] == join_step["node_id"])


def main() -> int:
    flows = build_fixture_flows()
    verify_step_chaining(flows)
    print(f"PASS: row-flow evaluated {len(flows)} fixture queries")
    print("PASS: every step input matches the previous step output")
    verify_full_scan_filter(flows["01_full_scan"])
    print("PASS: 01_full_scan predicate flags match manual amount > 500 evaluation")
    verify_sort_limit(flows["05_sort_limit"])
    print("PASS: 05_sort_limit preserves all rows before Limit and cuts to at most 10")
    verify_zero_rows_skips_join(flows["07_dead_end"])
    print("PASS: 07_dead_end filters all sample rows and skips its join")
    print("ROW-FLOW TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
