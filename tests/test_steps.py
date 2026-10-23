from pathlib import Path

import pytest

from core.parser import parse_explain_analyze
from core.steps import build_animation_steps


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "demo" / "plans_fallback").glob("*.txt"))


def _nodes(tree):
    yield tree
    for child in tree["children"]:
        yield from _nodes(child)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_animation_steps_are_post_order_and_cover_each_node(path):
    tree = parse_explain_analyze(path.read_text(encoding="utf-8"))
    steps = build_animation_steps(tree)
    nodes = list(_nodes(tree))
    by_id = {step["node_id"]: step for step in steps}

    assert len(steps) == len(nodes)
    assert len(by_id) == len(steps)
    assert [step["step"] for step in steps] == list(range(1, len(steps) + 1))
    assert steps[-1]["node_id"] == tree["id"]
    assert steps[-1]["parent_id"] is None
    for node in nodes:
        parent_step = by_id[node["id"]]["step"]
        for child in node["children"]:
            assert by_id[child["id"]]["step"] < parent_step
            assert by_id[child["id"]]["parent_id"] == node["id"]
        step = by_id[node["id"]]
        assert step["rows_out"] == node["rows_total"]
        assert step["loops"] == node["loops"]
        assert step["never_executed"] is node["never_executed"]


def test_07_animation_keeps_never_executed_node_with_zero_output():
    path = ROOT / "demo" / "plans_fallback" / "07_zero_rows.txt"
    tree = parse_explain_analyze(path.read_text(encoding="utf-8"))
    steps = build_animation_steps(tree)
    assert any(step["never_executed"] and step["rows_out"] == 0 for step in steps)


def test_action_mapping_covers_plan_operator_families():
    path = ROOT / "demo" / "plans_fallback" / "08_full_pipeline.txt"
    steps = build_animation_steps(parse_explain_analyze(path.read_text(encoding="utf-8")))
    actions = {step["action"] for step in steps}
    assert {"scan", "filter", "join", "aggregate", "sort"}.issubset(actions)

    other_plan = "-> Hash\n    -> Table scan on t (actual time=0.1..0.2 rows=2 loops=1)"
    other_steps = build_animation_steps(parse_explain_analyze(other_plan))
    assert next(step for step in other_steps if step["node_id"] == "node_1")["action"] == "other"
