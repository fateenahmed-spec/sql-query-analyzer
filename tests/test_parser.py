import re
from pathlib import Path

import pytest

from core.parser import ParseError, parse_explain_analyze


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "demo" / "plans_fallback").glob("*.txt"))
EXTRA_FIXTURES = [
    ROOT / "tests" / "fixtures" / "loop_scan.txt",
    ROOT / "tests" / "fixtures" / "plans" / "03_join_indexed.txt",
    ROOT / "tests" / "fixtures" / "plans" / "08_hash_join.txt",
]
NODE_LINE = re.compile(r"^( *)-> (.*)$")
NUMBER = r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
ACTUAL = re.compile(rf"\s+\(actual time=({NUMBER})\.\.({NUMBER}) rows=({NUMBER}) loops=({NUMBER})\)$")
COST = re.compile(rf"\s+\(cost=({NUMBER}(?:\.\.{NUMBER})?) rows=({NUMBER})\)$")


def _as_number(value):
    number = float(value) if "." in value or "e" in value.casefold() else int(value)
    return int(number) if isinstance(number, float) and number.is_integer() else number


def _raw_records(text):
    """Independently extract expected structure and statistics from source lines."""
    records = []
    for line_no, raw_line in enumerate(text.replace("\r\n", "\n").splitlines(), 1):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        match = NODE_LINE.match(line)
        assert match, f"fixture line {line_no} is not a node"
        indent, body = match.groups()
        never = body.endswith("(never executed)")
        if never:
            body = body[: -len("(never executed)")].rstrip()
        actual_match = ACTUAL.search(body)
        if actual_match:
            body = ACTUAL.sub("", body).rstrip()
            first, last, actual_rows, loops = map(_as_number, actual_match.groups())
        else:
            first = last = actual_rows = loops = None
        cost_match = COST.search(body)
        if cost_match:
            body = COST.sub("", body).rstrip()
            cost, estimated_rows = cost_match.groups()
            estimated_cost = _as_number(cost.rsplit("..", 1)[-1])
            estimated_rows = _as_number(estimated_rows)
        else:
            estimated_cost = estimated_rows = None
        if never:
            first = last = actual_rows = loops = 0
        rows_total = 0 if never else actual_rows * loops if actual_rows is not None and loops is not None else None
        time_total = 0 if never else last * loops if last is not None and loops is not None else None
        records.append(
            {
                "line_no": line_no,
                "depth": len(indent) // 4,
                "estimated_cost": estimated_cost,
                "estimated_rows": estimated_rows,
                "time_first_ms": first,
                "time_last_ms": last,
                "actual_rows": actual_rows,
                "loops": loops,
                "rows_total": rows_total,
                "time_total_ms": time_total,
                "never_executed": never,
                "children": [],
            }
        )
    stack = []
    for index, record in enumerate(records):
        depth = record["depth"]
        if depth:
            stack[depth - 1]["children"].append(index)
        stack = stack[:depth]
        stack.append(record)
    return records


def _expected_self_times(records):
    def expected(index):
        record = records[index]
        for child in record["children"]:
            expected(child)
        total = record["time_total_ms"]
        if total is None:
            record["accounted_time"] = sum(records[child]["accounted_time"] for child in record["children"])
            record["self_time_ms"] = 0
        else:
            record["self_time_ms"] = max(0, total - sum(records[child]["accounted_time"] for child in record["children"]))
            record["accounted_time"] = total
        return record["accounted_time"]

    expected(0)


def _flatten(tree):
    nodes = [tree]
    for child in tree["children"]:
        nodes.extend(_flatten(child))
    return nodes


@pytest.mark.parametrize("path", FIXTURES + EXTRA_FIXTURES, ids=lambda path: path.stem)
def test_parser_matches_independently_read_raw_fixture(path):
    text = path.read_text(encoding="utf-8")
    expected = _raw_records(text)
    root = parse_explain_analyze(text)
    actual = _flatten(root)

    assert len(actual) == sum(1 for line in text.splitlines() if "->" in line)
    assert len(actual) == len(expected)
    assert [node["id"] for node in actual] == [f"node_{i}" for i in range(1, len(actual) + 1)]
    assert len({node["id"] for node in actual}) == len(actual)

    for index, (node, source) in enumerate(zip(actual, expected)):
        for field in (
            "depth", "estimated_cost", "estimated_rows", "actual_rows", "loops",
            "rows_total", "time_first_ms", "time_last_ms", "time_total_ms", "never_executed",
        ):
            assert node[field] == source[field], (path.name, index, field)
        assert node["self_time_ms"] >= 0
        if node["time_last_ms"] is not None:
            assert node["time_total_ms"] == pytest.approx(node["time_last_ms"] * node["loops"])
        for child in node["children"]:
            assert child["depth"] == node["depth"] + 1

    raw_expected = _raw_records(text)
    _expected_self_times(raw_expected)
    for node, source in zip(actual, raw_expected):
        assert node["self_time_ms"] == pytest.approx(source["self_time_ms"])
    self_sum = sum(node["self_time_ms"] for node in actual)
    root_time = root["time_total_ms"] or 0
    assert self_sum >= root_time - 0.01
    clamped = any(
        record["time_total_ms"] is not None
        and record["time_total_ms"] < sum(raw_expected[child]["accounted_time"] for child in record["children"])
        for record in raw_expected
    )
    if not clamped:
        assert self_sum == pytest.approx(root_time, abs=0.01)


def test_08_full_pipeline_shape_and_hand_computed_self_times():
    path = ROOT / "demo" / "plans_fallback" / "08_full_pipeline.txt"
    tree = parse_explain_analyze(path.read_text(encoding="utf-8"))
    sort = tree
    temporary_scan = sort["children"][0]
    aggregate = temporary_scan["children"][0]
    join = aggregate["children"][0]
    filtered_scan, lookup = join["children"]

    assert sort["operator"] == "Sort"
    assert temporary_scan["operator"] == "Table scan" and temporary_scan["details"] == "on <temporary>"
    assert aggregate["operator"] == "Aggregate using temporary table"
    assert join["operator"] == "Nested loop inner join"
    assert filtered_scan["operator"] == "Filter"
    assert filtered_scan["children"][0]["operator"] == "Table scan"
    assert filtered_scan["children"][0]["details"] == "on o"
    assert lookup["operator"] == "Single-row index lookup"
    assert lookup["details"].startswith("on c using PRIMARY")
    assert len(join["children"]) == 2

    # Root: 14.310 - 14.260. Join: 13.200 - 5.800 - (0.0004 * 17,990).
    assert sort["self_time_ms"] == pytest.approx(0.05)
    assert join["self_time_ms"] == pytest.approx(0.204)


def test_07_never_executed_node_has_zero_runtime_and_rows():
    path = ROOT / "demo" / "plans_fallback" / "07_zero_rows.txt"
    nodes = _flatten(parse_explain_analyze(path.read_text(encoding="utf-8")))
    node = next(item for item in nodes if item["never_executed"])
    assert node["loops"] == 0
    assert node["rows_total"] == 0
    assert node["never_executed"] is True


@pytest.mark.parametrize(
    "raw_text,line_no",
    [
        ("  -> Table scan on t", 1),
        ("-> Root\n        -> Child", 2),
        ("-> Root\n-> Another root", 2),
        ("\n  \n", 0),
    ],
)
def test_parse_errors_include_line_number_and_offending_line(raw_text, line_no):
    with pytest.raises(ParseError) as exc_info:
        parse_explain_analyze(raw_text)
    assert exc_info.value.line_no == line_no
    assert isinstance(exc_info.value.offending_line, str)


def test_parentheses_and_quotes_in_details_are_not_mistaken_for_statistics():
    raw = "-> Filter: json_extract(doc, '$.x(y)') = 'literal (kept)'  (cost=2.5..9.0 rows=20) (actual time=0.1..0.2 rows=3 loops=1)"
    node = parse_explain_analyze(raw)
    assert node["operator"] == "Filter"
    assert node["details"] == "json_extract(doc, '$.x(y)') = 'literal (kept)'"
    assert node["estimated_cost"] == 9
    assert node["estimated_rows"] == 20
    assert node["actual_rows"] == 3


def test_scientific_notation_parses_for_estimates_actuals_and_loops():
    node = parse_explain_analyze(
        "-> Index lookup on t using i (id=1) (cost=1.2e-3 rows=6e-1) "
        "(actual time=1e-3..2.5E+2 rows=1.37e+2 loops=1e1)"
    )

    assert node["estimated_cost"] == pytest.approx(0.0012)
    assert node["estimated_rows"] == pytest.approx(0.6)
    assert node["time_first_ms"] == pytest.approx(0.001)
    assert node["time_last_ms"] == pytest.approx(250)
    assert node["actual_rows"] == 137
    assert node["loops"] == 10
    assert node["rows_total"] == 1370
    assert node["time_total_ms"] == pytest.approx(2500)
