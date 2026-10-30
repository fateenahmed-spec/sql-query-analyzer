from __future__ import annotations

import sqlite3

from row_flow import build_row_flow


class FakeCursor:
    def __init__(self):
        self.rows = []
        self.description = None
        self.statements = []
        self._database = [("sql_analyzer_test",)]

    def execute(self, statement, params=None):
        self.statements.append(statement)
        normalized = statement.strip().casefold()
        if normalized == "select database()":
            self.rows = list(self._database)
            return
        if normalized.startswith("select ("):
            expression = statement[len("SELECT ("):-1].replace("%s", "?")
            with sqlite3.connect(":memory:") as connection:
                self.rows = [connection.execute(f"SELECT ({expression})", tuple(params or ())).fetchone()]
            return
        self.rows = []
        if "limit 0" in normalized and "customers" in normalized:
            self.description = [("id",), ("name",)]
        else:
            self.description = None

    def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.fake_cursor = FakeCursor()

    def cursor(self):
        return self.fake_cursor


def _node(node_id, operator, details="", children=(), rows=0):
    return {
        "id": node_id,
        "operator": operator,
        "details": details,
        "children": list(children),
        "rows_total": rows,
        "actual_rows": rows,
        "estimated_rows": rows,
        "loops": 1,
        "never_executed": False,
    }


def _run(query, plan, samples, final_rows=None):
    plan["_flow_query"] = query
    connection = FakeConnection()
    steps = build_row_flow(plan, connection, samples, final_rows)
    records = {}
    for step in steps:
        records.update(step["row_data"])
    return steps, records, connection.fake_cursor


def _step(steps, node_id):
    return next(step for step in steps if step["node_id"] == node_id)


def test_simple_select_uses_original_result_and_keeps_source_sample():
    source = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    plan = _node("scan", "Table scan", "on customers", rows=2)
    steps, records, _ = _run(
        "SELECT id, name FROM customers", plan, {"customers": source},
        [{"id": 2, "name": "B"}],
    )

    scan = _step(steps, "scan")
    assert scan["exactness"] == "actual_query_result"
    assert scan["source_sample_rids"]
    assert records[scan["output_rids"][0]]["values"] == {"id": 2, "name": "B"}
    assert records[scan["source_sample_rids"][0]]["values"] == source[0]


def test_where_filters_rows_instead_of_scaling_from_plan_counts():
    scan = _node("scan", "Table scan", "on o", rows=3)
    plan = _node("filter", "Filter", "o.amount > 10", [scan], rows=1)
    steps, records, _ = _run(
        "SELECT * FROM orders o WHERE o.amount > 10", plan,
        {"o (sql_analyzer_test.orders)": [
            {"id": 1, "amount": 5}, {"id": 2, "amount": 15}, {"id": 3, "amount": 25},
        ]},
    )

    filtered = _step(steps, "filter")
    assert filtered["input_rids"] == ["o#1", "o#2", "o#3"]
    assert filtered["output_rids"] == ["o#2", "o#3"]
    assert filtered["row_results"] == {"o#1": False, "o#2": True, "o#3": True}
    assert filtered["exactness"] == "exact_for_sample"
    assert records["o#1"]["values"]["amount"] == 5


def test_multiple_filters_chain_their_own_child_output_sets():
    scan = _node("scan", "Table scan", "on o", rows=4)
    first = _node("filter_1", "Filter", "o.amount > 10", [scan], rows=3)
    second = _node("filter_2", "Filter", "o.amount < 30", [first], rows=2)
    steps, _, _ = _run(
        "SELECT * FROM orders o WHERE o.amount > 10 AND o.amount < 30", second,
        {"o (sql_analyzer_test.orders)": [
            {"id": 1, "amount": 5}, {"id": 2, "amount": 15},
            {"id": 3, "amount": 25}, {"id": 4, "amount": 35},
        ]},
    )

    one = _step(steps, "filter_1")
    two = _step(steps, "filter_2")
    assert one["output_rids"] == ["o#2", "o#3", "o#4"]
    assert two["input_rids"] == one["output_rids"]
    assert two["output_rids"] == ["o#2", "o#3"]


def test_inner_join_combines_rows_from_independent_plan_branches():
    orders = _node("orders", "Table scan", "on o", rows=2)
    customers = _node("customers", "Table scan", "on c", rows=2)
    plan = _node("join", "Nested loop inner join", children=[orders, customers], rows=1)
    steps, records, _ = _run(
        "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id",
        plan,
        {
            "o (sql_analyzer_test.orders)": [{"id": 10, "customer_id": 1}, {"id": 11, "customer_id": 2}],
            "c (sql_analyzer_test.customers)": [{"id": 1, "name": "A"}, {"id": 3, "name": "C"}],
        },
    )

    joined = _step(steps, "join")
    assert [branch["node_id"] for branch in joined["input_branches"]] == ["orders", "customers"]
    assert len(joined["output_rids"]) == 1
    assert joined["matched_pairs"] == [["o#1", "c#1"]]
    assert records[joined["output_rids"][0]]["values"] == {"o.id": 10, "o.customer_id": 1, "c.id": 1, "c.name": "A"}


def test_left_join_keeps_unmatched_left_row_with_null_right_values():
    orders = _node("orders", "Table scan", "on o", rows=1)
    customers = _node("customers", "Table scan", "on c", rows=1)
    plan = _node("join", "Nested loop left join", children=[orders, customers], rows=1)
    steps, records, _ = _run(
        "SELECT o.id, c.name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id",
        plan,
        {
            "o (sql_analyzer_test.orders)": [{"id": 10, "customer_id": 9}],
            "c (sql_analyzer_test.customers)": [{"id": 1, "name": "A"}],
        },
    )

    joined = _step(steps, "join")
    assert len(joined["output_rids"]) == 1
    assert joined["matched_pairs"] == [["o#1", None]]
    assert records[joined["output_rids"][0]]["values"]["c.name"] is None


def test_left_join_preserves_left_rows_when_right_source_sample_is_empty():
    orders = _node("orders", "Table scan", "on o", rows=1)
    customers = _node("customers", "Table scan", "on c", rows=0)
    plan = _node("join", "Nested loop left join", children=[orders, customers], rows=1)
    steps, records, _ = _run(
        "SELECT o.id, c.name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id",
        plan,
        {
            "o (sql_analyzer_test.orders)": [{"id": 10, "customer_id": 9}],
            "c (sql_analyzer_test.customers)": [],
        },
    )

    joined = _step(steps, "join")
    assert joined["evaluable"] is True
    assert len(joined["output_rids"]) == 1
    assert records[joined["output_rids"][0]]["values"]["c.name"] is None


def test_group_aggregate_is_computed_over_samples_and_labeled_as_sampled():
    scan = _node("scan", "Table scan", "on o", rows=4)
    plan = _node("aggregate", "Aggregate using temporary table", children=[scan], rows=2)
    steps, records, _ = _run(
        "SELECT customer_id, SUM(amount) AS total FROM orders o GROUP BY customer_id",
        plan,
        {"o (sql_analyzer_test.orders)": [
            {"customer_id": 1, "amount": 5}, {"customer_id": 1, "amount": 7},
            {"customer_id": 2, "amount": 3}, {"customer_id": 2, "amount": 4},
        ]},
    )

    aggregate = _step(steps, "aggregate")
    assert aggregate["exactness"] == "sampled_simulation"
    assert aggregate["output_rids"]
    assert {tuple(records[rid]["values"].items()) for rid in aggregate["output_rids"]} == {
        (("customer_id", 1), ("total", 12)), (("customer_id", 2), ("total", 7)),
    }
    assert "not the full-query aggregate" in aggregate["note"]


def test_having_filter_uses_calculated_sample_aggregate_values():
    scan = _node("scan", "Table scan", "on o", rows=3)
    aggregate = _node("aggregate", "Aggregate", children=[scan], rows=2)
    plan = _node("having", "Filter", "sum(o.amount) > 10", [aggregate], rows=1)
    steps, records, _ = _run(
        "SELECT customer_id, SUM(amount) AS total FROM orders o "
        "GROUP BY customer_id HAVING SUM(amount) > 10",
        plan,
        {"o (sql_analyzer_test.orders)": [
            {"customer_id": 1, "amount": 5}, {"customer_id": 1, "amount": 7},
            {"customer_id": 2, "amount": 3},
        ]},
    )

    filtered = _step(steps, "having")
    assert filtered["evaluable"] is True
    assert filtered["output_rids"] == ["aggregate#sample1"]
    assert records[filtered["output_rids"][0]]["values"]["total"] == 12


def test_cte_materialization_preserves_child_rows_without_claiming_physical_identity():
    scan = _node("scan", "Table scan", "on o", rows=2)
    plan = _node("materialize", "Materialize", children=[scan], rows=2)
    steps, _, _ = _run(
        "WITH recent AS (SELECT * FROM orders o) SELECT * FROM recent",
        plan,
        {"o (sql_analyzer_test.orders)": [{"id": 1}, {"id": 2}]},
    )

    materialize = _step(steps, "materialize")
    assert materialize["output_rids"] == materialize["input_rids"]
    assert materialize["exactness"] == "sample_passthrough"
    assert "physical wrapper" in materialize["note"]


def test_nested_ctes_and_derived_tables_keep_base_sources_and_branch_links():
    scan = _node("scan", "Table scan", "on o", rows=2)
    derived = _node("derived", "Materialize", children=[scan], rows=2)
    plan = _node("root", "Materialize", children=[derived], rows=2)
    steps, records, _ = _run(
        "WITH base_rows AS (SELECT * FROM orders o), recent AS (SELECT * FROM base_rows) "
        "SELECT * FROM (SELECT * FROM recent) d",
        plan,
        {"o (sql_analyzer_test.orders)": [{"id": 1}, {"id": 2}]},
        [{"id": 2}],
    )

    scan_step = _step(steps, "scan")
    derived_step = _step(steps, "derived")
    root = _step(steps, "root")
    assert scan_step["output_rids"] == derived_step["input_rids"]
    assert derived_step["output_rids"] == root["input_rids"]
    assert root["exactness"] == "actual_query_result"
    assert records[root["output_rids"][0]]["values"] == {"id": 2}


def test_correlated_semijoin_uses_two_branches_and_the_correlated_equality():
    customers = _node("customers", "Table scan", "on c", rows=2)
    orders = _node("orders", "Table scan", "on o", rows=2)
    plan = _node("semi", "Nested loop semijoin", children=[customers, orders], rows=1)
    steps, records, _ = _run(
        "SELECT c.id FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.id)",
        plan,
        {
            "c (sql_analyzer_test.customers)": [{"id": 1}, {"id": 2}],
            "o (sql_analyzer_test.orders)": [{"customer_id": 2}, {"customer_id": 2}],
        },
    )

    semi = _step(steps, "semi")
    assert semi["evaluable"] is True
    assert semi["output_rids"]
    assert len(semi["output_rids"]) == 1
    assert records[semi["output_rids"][0]]["values"]["id"] == 2


def test_uncorrelated_in_subquery_uses_the_materialized_semijoin_branch():
    customers = _node("customers", "Table scan", "on c", rows=2)
    orders = _node("orders", "Table scan", "on o", rows=2)
    plan = _node("semi", "Nested loop semijoin", children=[customers, orders], rows=1)
    steps, records, _ = _run(
        "SELECT c.id FROM customers c WHERE c.id IN (SELECT o.customer_id FROM orders o)",
        plan,
        {
            "c (sql_analyzer_test.customers)": [{"id": 1}, {"id": 2}],
            "o (sql_analyzer_test.orders)": [{"customer_id": 2}, {"customer_id": 2}],
        },
    )

    semi = _step(steps, "semi")
    assert semi["evaluable"] is True
    assert len(semi["output_rids"]) == 1
    assert records[semi["output_rids"][0]]["values"]["id"] == 2


def test_unsupported_uncorrelated_subquery_filter_is_explained_without_invented_rows():
    scan = _node("scan", "Table scan", "on c", rows=20)
    plan = _node("filter", "Filter", "c.id IN (SELECT o.customer_id FROM orders o)", [scan], rows=4)
    steps, _, _ = _run(
        "SELECT * FROM customers c WHERE c.id IN (SELECT o.customer_id FROM orders o)",
        plan,
        {"c (sql_analyzer_test.customers)": [{"id": 1}, {"id": 2}, {"id": 3}]},
    )

    filtered = _step(steps, "filter")
    assert filtered["output_rids"] == []
    assert filtered["exactness"] == "not_evaluable"
    assert "subquery" in filtered["note"]


def test_unknown_scalar_function_is_not_executed_for_sample_evaluation():
    scan = _node("scan", "Table scan", "on o", rows=1)
    plan = _node("filter", "Filter", "custom_row_check(o.amount) > 1", [scan], rows=1)
    steps, _, cursor = _run(
        "SELECT * FROM orders o WHERE custom_row_check(o.amount) > 1",
        plan,
        {"o (sql_analyzer_test.orders)": [{"amount": 2}]},
    )

    filtered = _step(steps, "filter")
    assert filtered["output_rids"] == []
    assert filtered["exactness"] == "not_evaluable"
    assert "unsupported SQL expression" in filtered["note"]
    assert not any(statement.strip().casefold().startswith("select (") for statement in cursor.statements)


def test_multiple_joins_preserve_the_composite_left_branch():
    orders = _node("orders", "Table scan", "on o", rows=1)
    customers = _node("customers", "Table scan", "on c", rows=1)
    first_join = _node("join_1", "Nested loop inner join", children=[orders, customers], rows=1)
    items = _node("items", "Table scan", "on i", rows=2)
    plan = _node("join_2", "Nested loop inner join", children=[first_join, items], rows=1)
    steps, records, _ = _run(
        "SELECT o.id, c.name, i.sku FROM orders o "
        "JOIN customers c ON c.id = o.customer_id "
        "JOIN items i ON i.order_id = o.id",
        plan,
        {
            "o (sql_analyzer_test.orders)": [{"id": 10, "customer_id": 1}],
            "c (sql_analyzer_test.customers)": [{"id": 1, "name": "A"}],
            "i (sql_analyzer_test.items)": [{"order_id": 10, "sku": "x"}, {"order_id": 99, "sku": "y"}],
        },
    )

    first = _step(steps, "join_1")
    second = _step(steps, "join_2")
    assert second["input_branches"][0]["output_rids"] == first["output_rids"]
    assert second["input_branches"][1]["output_rids"] == ["i#1", "i#2"]
    assert len(second["output_rids"]) == 1
    assert records[second["output_rids"][0]]["values"]["i.sku"] == "x"


def test_self_join_without_plan_aliases_does_not_choose_an_arbitrary_source_sample():
    first_scan = _node("first", "Table scan", "on users", rows=1)
    second_scan = _node("second", "Table scan", "on users", rows=1)
    plan = _node("join", "Nested loop inner join", children=[first_scan, second_scan], rows=1)
    steps, _, _ = _run(
        "SELECT a.id, b.id FROM users a JOIN users b ON a.manager_id = b.id",
        plan,
        {
            "a (sql_analyzer_test.users)": [{"id": 1, "manager_id": 2}],
            "b (sql_analyzer_test.users)": [{"id": 2, "manager_id": 1}],
        },
        [{"id": 1}],
    )

    joined = _step(steps, "join")
    assert [branch["exactness"] for branch in joined["input_branches"]] == ["not_evaluable", "not_evaluable"]
    assert all(not branch["output_rids"] for branch in joined["input_branches"])
    assert joined["matched_pairs"] == []
    assert joined["exactness"] == "actual_query_result"
    assert joined["output_rids"] == ["query_result#1"]


def test_window_function_adds_sample_calculation_beside_original_rows():
    scan = _node("scan", "Table scan", "on t", rows=3)
    plan = _node("window", "Window aggregate", children=[scan], rows=3)
    steps, records, _ = _run(
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY grp ORDER BY amount DESC) AS rn FROM items t",
        plan,
        {"t (sql_analyzer_test.items)": [
            {"id": 1, "grp": "a", "amount": 5}, {"id": 2, "grp": "a", "amount": 9},
            {"id": 3, "grp": "b", "amount": 2},
        ]},
    )

    window = _step(steps, "window")
    assert window["exactness"] == "sampled_simulation"
    assert [records[rid]["values"]["rn"] for rid in window["output_rids"]] == [2, 1, 1]
    assert all("id" in records[rid]["values"] for rid in window["output_rids"])


def test_unknown_operator_does_not_infer_survivors_from_plan_counts():
    scan = _node("scan", "Table scan", "on t", rows=10)
    plan = _node("unknown", "Optimizer-specific operator", children=[scan], rows=2)
    steps, _, _ = _run(
        "SELECT * FROM items t",
        plan,
        {"t (sql_analyzer_test.items)": [{"id": i} for i in range(10)]},
    )

    unknown = _step(steps, "unknown")
    assert unknown["output_rids"] == []
    assert unknown["exactness"] == "not_evaluable"
    assert "No output rows were inferred" in unknown["note"]


def test_materialize_with_deduplication_is_not_mislabeled_as_passthrough():
    scan = _node("scan", "Table scan", "on t", rows=2)
    plan = _node("materialize", "Materialize with deduplication", children=[scan], rows=1)
    steps, _, _ = _run(
        "SELECT DISTINCT id FROM items t",
        plan,
        {"t (sql_analyzer_test.items)": [{"id": 1}, {"id": 1}]},
    )

    materialize = _step(steps, "materialize")
    assert materialize["op"] == "other"
    assert materialize["output_rids"] == []
    assert materialize["exactness"] == "not_evaluable"
