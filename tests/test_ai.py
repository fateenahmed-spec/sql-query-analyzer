import json

import pytest
import requests

from core import ai
from core.parser import parse_explain_analyze
from core.rules import analyze_rules

API_KEY = "test-secret-gemini-key"
QUERY = "SELECT * FROM orders WHERE amount > 500"
WARNINGS = [{
    "code": "FULL_SCAN",
    "classification": "observation",
    "severity": "low",
    "title": "MySQL scanned many rows",
    "message": "A full scan can be efficient when the query needs many rows.",
    "evidence": "Table scan on orders · 10 rows read · 1 loop · self time 2.5 ms.",
    "suggestion": "If the query is slow or should return few rows, inspect its filters and available indexes.",
}]
TREE = {
    "id": "n1",
    "operator": "Table scan",
    "details": "Table scan on orders",
    "estimated_rows": 12,
    "actual_rows": 10,
    "loops": 1,
    "rows_total": 10,
    "time_total_ms": 2.5,
    "self_time_ms": 2.5,
    "children": [],
}
GOOD_RESPONSE_TEXT = json.dumps({
    "summary": "This query reads orders and filters them by amount. MySQL follows the plan to return matching rows.",
    "finding": "The available plan does not show a clear performance concern.",
    "suggestion": "No meaningful query change is needed based on the current execution plan.",
})

@pytest.fixture
def ai_setup(monkeypatch, mocker):
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    mocker.patch("core.ai._get_cached", return_value=None)
    mocker.patch("core.ai._store_cached")
    mocker.patch("core.ai.time.sleep")
    post = mocker.patch("core.ai.requests.post")
    return post

def _response(mocker, text=GOOD_RESPONSE_TEXT, status=200, *, candidates=True, finish="STOP"):
    body = {}
    if candidates:
        body["candidates"] = [{
            "finishReason": finish,
            "content": {"parts": [{"text": text}]},
        }]
    response = mocker.Mock(status_code=status)
    response.json.return_value = body
    return response

def _submitted_ai_context(post):
    prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
    return json.loads(prompt.split("DATA:\n", 1)[1])

def _run_and_check_secret(caplog, capsys):
    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)
    response_body = json.dumps(result, ensure_ascii=False)
    output = capsys.readouterr()
    assert API_KEY not in response_body
    assert API_KEY not in caplog.text
    assert API_KEY not in output.out
    assert API_KEY not in output.err
    return result

def test_rest_success_sends_key_only_in_header(ai_setup, mocker, caplog, capsys):
    ai_setup.return_value = _response(mocker)

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "success"
    assert result["summary"].startswith("This query reads orders")
    assert result["finding"]
    assert result["suggestions"] == [ai._NO_CHANGE]
    assert result["suggestion"] == ai._NO_CHANGE
    assert "fix" not in result
    assert "suggested_query" not in result
    args, kwargs = ai_setup.call_args
    assert args[0] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-test-model:generateContent"
    )
    assert API_KEY not in args[0]
    assert kwargs["headers"]["x-goog-api-key"] == API_KEY
    assert kwargs["timeout"] == (10, 30)
    assert API_KEY not in json.dumps(kwargs["json"])
    assert set(kwargs["json"]) == {"contents", "generationConfig"}
    prompt = kwargs["json"]["contents"][0]["parts"][0]["text"]
    data = json.loads(prompt.split("DATA:\n", 1)[1])
    assert set(data) == {
        "sql", "query_truncated", "parsed_plan", "schema_info", "warnings", "rule_flags",
        "database_error", "database_error_truncated",
        "warnings_truncated", "warning_fields_truncated", "warnings_complete",
        "evidence_complete", "evidence_notice",
    }
    assert data["schema_info"]["tables_in_query"] == ["orders"]
    assert data["schema_info"]["complete_index_catalog_available"] is False
    plan_node = data["parsed_plan"]["nodes"][0]
    assert plan_node["estimated_rows_per_loop"] == 12
    assert plan_node["actual_rows_per_loop"] == 10
    assert plan_node["loops"] == 1
    assert plan_node["elapsed_ms_total"] == 2.5
    assert "inclusive_time_ms" not in plan_node
    assert plan_node["details_truncated"] is False
    assert data["parsed_plan"]["complete"] is True
    assert data["warnings_complete"] is True
    assert data["evidence_complete"] is True
    assert data["warnings"] == []
    assert data["rule_flags"] == []
    assert "eligible_rewrite_candidate" not in data
    assert "rewritten SQL" in prompt
    assert "do not provide rewritten sql" in prompt.lower()
    assert "SQL code" in prompt
    assert "physical disk blocks" in prompt.lower()
    assert "temporary table is not automatically slow" in prompt.lower()
    assert "never claim an index is absent or recommend creating one" in prompt.lower()

def test_retries_429_then_succeeds(ai_setup, mocker, caplog, capsys):
    ai_setup.side_effect = [
        _response(mocker, status=429),
        _response(mocker),
    ]

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "success"
    assert ai_setup.call_count == 2

def test_gemini_rate_limit_falls_back_after_retries(ai_setup, mocker, caplog, capsys):
    ai_setup.side_effect = [_response(mocker, status=429) for _ in range(3)]

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini rate limited"
    assert ai_setup.call_count == 3

def test_missing_gemini_key_uses_rule_fallback(monkeypatch, mocker):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    post = mocker.patch("core.ai.requests.post")

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "API key missing"
    assert result["summary"]
    post.assert_not_called()

def test_retries_500_twice_then_falls_back(ai_setup, mocker, caplog, capsys):
    ai_setup.side_effect = [_response(mocker, status=500) for _ in range(3)]

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini service unavailable"
    assert ai_setup.call_count == 3

def test_malformed_model_json_falls_back(ai_setup, mocker, caplog, capsys):
    ai_setup.return_value = _response(mocker, text="{malformed")

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "invalid Gemini response"

@pytest.mark.parametrize(
    ("body", "expected_reason"),
    [
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "Gemini blocked the prompt"),
        ({}, "Gemini returned no candidates"),
    ],
)
def test_blocked_prompt_or_missing_candidates_falls_back(
    ai_setup, mocker, caplog, capsys, body, expected_reason
):
    response = mocker.Mock(status_code=200)
    response.json.return_value = body
    ai_setup.return_value = response

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == expected_reason

def test_non_stop_finish_reason_falls_back(ai_setup, mocker, caplog, capsys):
    ai_setup.return_value = _response(mocker, finish="MAX_TOKENS")

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini response incomplete"

def test_winerror_10013_connection_error_falls_back(ai_setup, caplog, capsys):
    ai_setup.side_effect = requests.exceptions.ConnectionError(
        "[WinError 10013] Permission denied"
    )

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "network blocked"

def test_timeout_falls_back(ai_setup, caplog, capsys):
    ai_setup.side_effect = requests.exceptions.Timeout("timed out")

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini API timed out. Using offline fallback."
    assert ai_setup.call_count == 2

def test_simple_query_without_plan_concern_has_no_optimization_advice():
    tree = parse_explain_analyze(
        "-> Single-row index lookup on customers using PRIMARY (id=7) "
        "(cost=0.25 rows=1) (actual time=0.01..0.02 rows=1 loops=1)"
    )
    result = ai.get_rule_based_explanation(
        "SELECT name FROM customers WHERE id = 7",
        [],
        tree_summary=tree,
    )

    assert "reads rows from `customers`" in result["summary"]
    assert "filters rows" in result["summary"]
    assert result["suggestions"] == [ai._NO_CHANGE]
    assert "clear performance concern" in result["finding"]

def test_small_table_scan_is_not_automatically_called_inefficient():
    tree = parse_explain_analyze(
        "-> Table scan on small_lookup (cost=1 rows=12) "
        "(actual time=0.01..0.03 rows=12 loops=1)"
    )
    warnings = analyze_rules(tree)
    result = ai.get_rule_based_explanation(
        "SELECT id FROM small_lookup",
        warnings,
        tree_summary=tree,
    )

    assert "FULL_SCAN" not in {warning["code"] for warning in warnings}
    assert result["suggestions"] == [ai._NO_CHANGE]
    assert "disk blocks" not in result["finding"].lower()

def test_selective_filter_turns_generic_index_advice_into_a_careful_check(ai_setup, mocker):
    query = "SELECT id FROM orders WHERE status = 'open'"
    tree = parse_explain_analyze(
        "-> Filter: (orders.status = 'open') (cost=10 rows=10) (actual time=0.1..5 rows=10 loops=1)\n"
        "    -> Table scan on orders (cost=10 rows=5000) (actual time=0.1..20 rows=5000 loops=1)"
    )
    warnings = analyze_rules(tree)
    assert "FILTER_DISCARDS_MOST" in {warning["code"] for warning in warnings}
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "MySQL filters orders to find rows with an open status.",
        "finding": "The filter kept a small share of the rows read.",
        "suggestion": "Create a composite index on orders(status) to avoid scanning rows.",
    }))

    result = ai.get_ai_explanation(query, warnings, tree_summary=tree)

    assert result["ai_status"] == "success"
    assert len(result["suggestions"]) == 1
    recommendation = result["suggestions"][0].lower()
    assert "if the query is slow" in recommendation
    assert "existing index supports this filter or join" in recommendation
    assert "full index list" in recommendation
    assert "storage" in recommendation and "inserts and updates" in recommendation
    assert "create a composite index" not in recommendation
    prompt = ai_setup.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
    context = json.loads(prompt.split("DATA:\n", 1)[1])
    filter_warning = next(warning for warning in context["warnings"] if warning["code"] == "FILTER_DISCARDS_MOST")
    assert "kept 10 of 5,000 input rows" in filter_warning["evidence"]

def test_temporary_table_is_explained_without_forcing_group_by_changes(ai_setup, mocker):
    query = "SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id"
    tree = parse_explain_analyze(
        "-> Aggregate using temporary table (cost=10 rows=5) (actual time=1..2 rows=5 loops=1)\n"
        "    -> Table scan on orders (cost=10 rows=20) (actual time=0.1..1 rows=20 loops=1)"
    )
    warnings = [warning for warning in analyze_rules(tree) if warning["code"] == "TEMP_TABLE"]
    assert warnings
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query groups order amounts by customer. MySQL uses an intermediate result for the grouping.",
        "finding": "A temporary table was used for aggregation.",
        "suggestion": "Optimize the GROUP BY keys.",
    }))

    result = ai.get_ai_explanation(query, warnings, tree_summary=tree)

    assert "temporary table is not automatically slow" in ai_setup.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"].lower()
    assert result["suggestions"] == [ai._NO_CHANGE]

def test_join_plan_primary_key_blocks_unneeded_composite_index_advice(ai_setup, mocker):
    query = "SELECT o.id, c.city FROM orders o JOIN customers c ON c.id = o.customer_id"
    plan = (ai._PROJECT_ROOT / "tests" / "fixtures" / "plans" / "03_join_indexed.txt").read_text(encoding="utf-8")
    tree = parse_explain_analyze(plan)
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query joins orders to customers using the customer ID.",
        "finding": "The plan uses the customers primary key for its lookup.",
        "suggestion": "Add a composite index on customers(id, city).",
    }))

    result = ai.get_ai_explanation(query, [], tree_summary=tree)

    assert result["suggestions"] == [ai._NO_CHANGE]
    prompt = ai_setup.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
    context = json.loads(prompt.split("DATA:\n", 1)[1])
    customer_primary = next(
        index for index in context["schema_info"]["indexes_observed_in_plan"]
        if index["table"] == "customers" and index["index"] == "PRIMARY"
    )
    assert customer_primary["plan_reference"] == "c"
    assert customer_primary["lookup_condition"] == "id=o.customer_id"

def test_gemini_cannot_add_an_unobserved_metric_to_the_finding(ai_setup, mocker):
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query took 999 ms to scan orders.",
        "finding": "MySQL read 999 rows.",
        "suggestion": "The query processes 999 rows per loop.",
    }))

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert "999" not in result["summary"]
    assert "999" not in result["finding"]
    assert result["suggestions"] == [ai._NO_CHANGE]
    assert result["suggestion"] == ai._NO_CHANGE
    assert "learning_point" not in result

def test_gemini_cannot_swap_estimated_rows_with_actual_rows(ai_setup, mocker):
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "MySQL estimated 10 rows but returned 12 rows.",
        "finding": "The plan estimated 10 rows and measured 12 actual rows.",
        "suggestion": "",
    }))

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert result["summary"] != "MySQL estimated 10 rows but returned 12 rows."
    assert result["finding"] != "The plan estimated 10 rows and measured 12 actual rows."

def test_gemini_cannot_claim_a_table_scan_reads_physical_disk_blocks(ai_setup, mocker):
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query performs a full table scan that reads unindexed disk blocks.",
        "finding": "Full table scans read physical disk blocks and are inefficient.",
        "suggestion": "",
    }))

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert "disk" not in result["summary"].lower()
    assert "disk" not in result["finding"].lower()
    assert "learning_point" not in result

def test_gemini_cannot_claim_sorting_happened_in_memory_without_plan_evidence(ai_setup, mocker):
    tree = parse_explain_analyze(
        "-> Sort: orders.city (cost=20 rows=5000) (actual time=1..80 rows=5000 loops=1)\n"
        "    -> Table scan on orders (cost=10 rows=5000) (actual time=0.1..30 rows=5000 loops=1)"
    )
    warnings = analyze_rules(tree)
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "MySQL scans orders and sorts the results in memory.",
        "finding": "The rows were sorted in memory.",
        "suggestion": "Check whether an in-memory sort can be avoided.",
    }))

    result = ai.get_ai_explanation(
        "SELECT city FROM orders ORDER BY city", warnings, tree_summary=tree
    )

    assert "memory" not in result["summary"].lower()
    assert "memory" not in result["finding"].lower()
    assert "learning_point" not in result
    assert result["suggestions"] == [ai._NO_CHANGE]

def test_estimate_mismatch_recommends_investigation(ai_setup, mocker):
    query = "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id"
    tree = parse_explain_analyze(
        "-> Nested loop inner join (cost=100 rows=219) (actual time=0.1..20 rows=2247 loops=1)\n"
        "    -> Filter: (orders.status = 'open') (cost=100 rows=219) (actual time=0.1..10 rows=2247 loops=1)\n"
        "        -> Table scan on orders (cost=100 rows=5000) (actual time=0.1..8 rows=5000 loops=1)\n"
        "    -> Single-row index lookup on customers using PRIMARY (id=orders.customer_id) "
        "(cost=0.25 rows=1) (actual time=0.01..0.02 rows=1 loops=2247)"
    )
    warnings = analyze_rules(tree)
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query groups matching orders by customer.",
        "finding": "The Filter and join each returned more rows than MySQL estimated.",
        "suggestion": "If the query is slow, check the relevant statistics and join values.",
    }))

    result = ai.get_ai_explanation(query, warnings, tree_summary=tree)

    assert {warning["code"] for warning in warnings}.issuperset({"ESTIMATE_MISMATCH"})
    assert result["suggestions"][0].startswith("If the query is slow")
    assert result["suggestion"].startswith("If the query is slow")
    assert "suggested_query" not in result
    assert "fix" not in result

def _node_scoped_metric_tree():
    return {
        "id": "root",
        "operator": "Sort",
        "details": "orders.salary DESC",
        "estimated_rows": 90,
        "actual_rows": 30,
        "rows_total": 60,
        "loops": 2,
        "time_first_ms": 1,
        "time_last_ms": 5,
        "time_total_ms": 10,
        "self_time_ms": 4,
        "self_time_pct": 40,
        "children": [{
            "id": "child",
            "operator": "Table scan",
            "details": "on orders",
            "estimated_rows": 100,
            "actual_rows": 10,
            "rows_total": 60,
            "loops": 6,
            "time_first_ms": 0.5,
            "time_last_ms": 5,
            "time_total_ms": 30,
            "self_time_ms": 10,
            "self_time_pct": 50,
            "children": [],
        }],
    }

def test_metric_validation_rejects_cross_node_attribution():
    tree = _node_scoped_metric_tree()

    assert ai._unsupported_metric_claim("The Sort returned 10 rows.", tree)
    assert not ai._unsupported_metric_claim("The Table scan returned 10 rows per loop.", tree)

def _multi_node_metric_claim_tree():
    return {
        "id": "filter",
        "operator": "Filter",
        "details": "(orders.status = 'open')",
        "estimated_rows": 20,
        "actual_rows": 5,
        "rows_total": 15,
        "loops": 3,
        "children": [{
            "id": "scan",
            "operator": "Table scan",
            "details": "on orders",
            "estimated_rows": 100,
            "actual_rows": 10,
            "rows_total": 60,
            "loops": 6,
            "children": [],
        }],
    }

def test_metric_validation_supports_multiple_node_claims_in_one_sentence():
    tree = _multi_node_metric_claim_tree()

    assert not ai._unsupported_metric_claim(
        "Table Scan returned 10 rows per loop and Filter returned 5 rows per loop.", tree
    )
    assert not ai._unsupported_metric_claim(
        "plan_node_2 returned 10 rows per loop and plan_node_1 returned 5 rows per loop.", tree
    )

def test_metric_validation_supports_per_loop_rows_loops_and_total_in_one_sentence():
    tree = _multi_node_metric_claim_tree()

    assert not ai._unsupported_metric_claim(
        "Filter produced 5 rows per loop over 3 loops, totaling 15 rows.", tree
    )

def test_metric_validation_rejects_wrong_node_references_and_values():
    tree = _multi_node_metric_claim_tree()

    assert ai._unsupported_metric_claim("Sort returned 5 rows per loop.", tree)
    assert ai._unsupported_metric_claim("plan_node_99 returned 5 rows per loop.", tree)
    assert ai._unsupported_metric_claim("Filter returned 10 rows per loop.", tree)
    assert ai._unsupported_metric_claim("Table Scan returned 5 rows per loop.", tree)

def test_metric_validation_rejects_a_matching_operator_with_the_wrong_table():
    tree = {
        "id": "root",
        "operator": "Sort",
        "details": "orders.salary DESC",
        "actual_rows": 90,
        "children": [{
            "id": "child",
            "operator": "Table scan",
            "details": "on customers",
            "actual_rows": 10,
            "loops": 1,
            "children": [],
        }],
    }

    assert ai._unsupported_metric_claim("Table Scan on orders returned 10 rows per loop.", tree)

def test_metric_validation_keeps_estimated_and_actual_rows_separate():
    tree = _node_scoped_metric_tree()

    assert not ai._unsupported_metric_claim("The Sort estimated 90 rows.", tree)
    assert ai._unsupported_metric_claim("The Sort estimated 30 rows.", tree)
    assert not ai._unsupported_metric_claim("The Sort produced 60 actual rows.", tree)
    assert ai._unsupported_metric_claim("The Sort produced 90 actual rows.", tree)

def test_metric_validation_keeps_actual_rows_per_loop_separate_from_total():
    tree = _node_scoped_metric_tree()

    assert not ai._unsupported_metric_claim("The Sort returned 30 rows per loop.", tree)
    assert ai._unsupported_metric_claim("The Sort returned 60 rows per loop.", tree)
    assert not ai._unsupported_metric_claim("The Sort returned 60 rows across loops.", tree)
    assert ai._unsupported_metric_claim("The Sort returned 30 rows across loops.", tree)

def test_metric_validation_keeps_inclusive_and_self_time_separate():
    tree = _node_scoped_metric_tree()

    assert not ai._unsupported_metric_claim("The Sort self time was 4 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort self time was 10 ms.", tree)
    assert not ai._unsupported_metric_claim("The Sort execution time was 10 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort execution time was 4 ms.", tree)
    assert not ai._unsupported_metric_claim("The Sort inclusive time was 10 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort inclusive time was 4 ms.", tree)
    assert not ai._unsupported_metric_claim("The Sort inclusive execution time was 10 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort inclusive execution time was 4 ms.", tree)

def test_metric_validation_keeps_first_and_last_row_times_separate():
    tree = _node_scoped_metric_tree()

    assert not ai._unsupported_metric_claim("The Sort first-row time was 1 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort first-row time was 5 ms.", tree)
    assert not ai._unsupported_metric_claim("The Sort last-row time per loop was 5 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort last-row time per loop was 1 ms.", tree)

def test_metric_validation_accepts_normal_rounding_and_grouped_numbers():
    tree = _node_scoped_metric_tree()
    tree["time_last_ms"] = 5.04
    tree["actual_rows"] = 1000
    tree["rows_total"] = 1000
    tree["loops"] = 1

    assert not ai._unsupported_metric_claim("The Sort last-row time per loop was 5.0 ms.", tree)
    assert ai._unsupported_metric_claim("The Sort last-row time per loop was 5.1 ms.", tree)
    assert not ai._unsupported_metric_claim("The Sort returned 1,000 rows across loops.", tree)

def test_metric_validation_checks_labelled_values_without_units_and_rejects_other_metrics():
    tree = _node_scoped_metric_tree()

    assert not ai._unsupported_metric_claim("The Sort estimated rows: 90.", tree)
    assert ai._unsupported_metric_claim("The Sort actual rows count: 10.", tree)
    assert ai._unsupported_metric_claim("The Sort estimated cost: 90.", tree)
    assert ai._unsupported_metric_claim("The Sort returned -30 rows.", tree)

def test_warning_classification_must_match_rule_evidence(ai_setup, mocker):
    warning = {
        "code": "ESTIMATE_MISMATCH",
        "classification": "observation",
        "severity": "high",
        "message": "The estimate differs from actual rows.",
        "evidence": "The estimate was much lower.",
        "suggestion": "Investigate if the query is slow.",
    }
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query reads orders.",
        "finding": "The row estimate is inaccurate and should be investigated.",
        "suggestion": "",
    }))

    result = ai.get_ai_explanation(QUERY, [warning], tree_summary=TREE)

    assert result["finding"] == "The available plan does not show a clear performance concern."

def _large_scan_warning_tree():
    return parse_explain_analyze(
        "-> Table scan on orders (cost=100 rows=5000) "
        "(actual time=0.1..30 rows=5000 loops=1)"
    )

@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("node_id", "node_999"),
        ("evidence", "Table scan on orders · 999999 rows read · 1 loop · self time 9999 ms."),
        ("severity", "high"),
        ("classification", "investigate"),
    ],
    ids=["mismatched-node-id", "unsupported-evidence", "incorrect-severity", "incorrect-classification"],
)
def test_ai_warning_context_is_derived_from_plan_not_caller_objects(
    ai_setup, mocker, field, forged_value
):
    tree = _large_scan_warning_tree()
    generated = analyze_rules(tree)
    assert generated and generated[0]["code"] == "FULL_SCAN"
    forged = dict(generated[0])
    forged[field] = forged_value
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation(QUERY, [forged], tree_summary=tree)

    prompt = ai_setup.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
    data = _submitted_ai_context(ai_setup)
    assert len(data["warnings"]) == 1
    assert data["warnings"][0]["code"] == generated[0]["code"]
    assert data["warnings"][0]["classification"] == generated[0]["classification"]
    assert data["warnings"][0]["severity"] == generated[0]["severity"]
    assert data["warnings"][0]["evidence"] == generated[0]["evidence"]
    assert str(forged_value) not in json.dumps(data["warnings"], ensure_ascii=False)

def test_live_ai_route_regenerates_warnings_instead_of_using_request_warnings(monkeypatch, mocker):
    import main

    tree = {"id": "node_1", "operator": "Table scan", "details": "on orders", "children": []}
    generated = [{
        "code": "FULL_SCAN",
        "classification": "observation",
        "severity": "low",
        "node_id": "node_1",
        "title": "Trusted server warning",
        "message": "Built from the plan tree.",
        "evidence": "Table scan on orders · 5000 rows read.",
        "suggestion": None,
    }]
    caller_warning = {**generated[0], "node_id": "node_999", "evidence": "unsupported evidence"}
    monkeypatch.setattr(main, "_public_rate_limit", lambda *_args: None)
    monkeypatch.setattr(main, "analyze_rules", lambda _actual_tree: generated)
    ai_call = mocker.patch(
        "main.get_ai_explanation",
        return_value={"ai_status": "success", "reason": "", "summary": "ok"},
    )

    main.get_live_ai_insights(
        main.AIRequest(query=QUERY, warnings=[caller_warning], tree=tree),
        mocker.Mock(),
    )

    assert ai_call.call_args.args[1] == generated
    assert ai_call.call_args.kwargs["tree_summary"] == tree

def test_warning_severity_and_recommendation_must_match_evidence(ai_setup, mocker):
    warning = {
        "code": "HOTSPOT",
        "classification": "investigate",
        "severity": "info",
        "message": "A step used the largest measured share of time.",
        "evidence": "Sort self time was a small share of total elapsed time.",
        "suggestion": "Investigate only if the overall query is slow.",
    }
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query reads orders.",
        "finding": "This is a high-severity hotspot.",
        "suggestion": "Update table statistics to fix the estimate.",
    }))

    result = ai.get_ai_explanation(QUERY, [warning], tree_summary=TREE)

    assert result["finding"] == "The available plan does not show a clear performance concern."
    assert result["suggestions"] == [ai._NO_CHANGE]

def test_informational_scan_warning_is_not_promoted_to_a_performance_problem(ai_setup, mocker):
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query reads orders.",
        "finding": "A full table scan is always inefficient and makes this query slow.",
        "suggestion": "Avoid the full table scan and add an index.",
    }))

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert result["finding"] == "The available plan does not show a clear performance concern."
    assert result["suggestions"] == [ai._NO_CHANGE]
    assert result["suggestion"] == ai._NO_CHANGE
    assert "learning_point" not in result

def _informational_temporary_table_tree():
    return {
        "id": "node_1",
        "operator": "Aggregate using temporary table",
        "details": "using temporary table",
        "actual_rows": 100,
        "rows_total": 100,
        "loops": 1,
        "time_total_ms": 10,
        "self_time_ms": 5,
        "children": [{
            "id": "node_2",
            "operator": "Table scan",
            "details": "on orders",
            "actual_rows": 100,
            "rows_total": 100,
            "loops": 1,
            "time_total_ms": 5,
            "self_time_ms": 5,
            "children": [],
        }],
    }

def test_informational_warning_cannot_be_promoted_to_a_high_priority_finding(ai_setup, mocker):
    tree = _informational_temporary_table_tree()
    warnings = analyze_rules(tree)
    assert any(item["code"] == "TEMP_TABLE" and item["severity"] == "info" for item in warnings)
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query uses a temporary table.",
        "finding": "The temporary table is a high-priority performance problem.",
        "suggestion": "Fix this high-priority temporary-table problem.",
    }))

    result = ai.get_ai_explanation(QUERY, warnings, tree_summary=tree)

    assert result["finding"] == "The available plan does not show a clear performance concern."
    assert result["suggestions"] == [ai._NO_CHANGE]

def test_never_executed_warning_gets_a_conditional_fallback_recommendation():
    tree = {
        "id": "node_1",
        "operator": "Index lookup",
        "details": "on orders using PRIMARY",
        "never_executed": True,
        "children": [],
    }
    warnings = analyze_rules(tree)

    result = ai.get_rule_based_explanation(QUERY, warnings, tree_summary=tree)

    assert "if you expected this step to run" in result["suggestions"][0].lower()
    assert "did not run" not in result["finding"].lower()

def test_gemini_and_fallback_expose_the_same_three_part_suggestion_output(ai_setup, mocker):
    query = (
        "SELECT e.id FROM employees AS e WHERE e.salary > "
        "(SELECT AVG(i.salary) FROM employees AS i WHERE i.department = e.department)"
    )
    ai_setup.return_value = _response(mocker)

    gemini_result = ai.get_ai_explanation(query, [], tree_summary=None)
    fallback_result = ai.get_rule_based_explanation(query, [])

    assert gemini_result["suggestion"] == fallback_result["suggestion"]
    assert gemini_result["summary"]
    assert gemini_result["finding"]
    assert "suggestion" in fallback_result
    assert "suggested_query" not in gemini_result
    assert "fix" not in fallback_result

def test_no_safe_suggestion_uses_the_requested_short_message(ai_setup, mocker):
    query = "SELECT id FROM orders WHERE amount > 500"
    ai_setup.return_value = _response(mocker)

    gemini_result = ai.get_ai_explanation(query, [], tree_summary=None)
    fallback_result = ai.get_rule_based_explanation(query, [])
    expected = "No meaningful query change is needed based on the current execution plan."

    assert gemini_result["suggestion"] == expected
    assert fallback_result["suggestion"] == expected

def test_demo_ai_route_passes_its_parsed_tree_to_fallback(monkeypatch, mocker):
    import main

    tree = {"id": "demo-root", "operator": "Table scan", "children": []}
    warnings = []
    fallback = mocker.patch("main.get_rule_based_explanation", return_value={"summary": "ok"})
    monkeypatch.setattr(main, "_public_rate_limit", lambda *_args: None)
    monkeypatch.setattr(main, "_read_queries", lambda: [{"id": "demo", "sql": "SELECT 1"}])
    monkeypatch.setattr(main, "load_plan_text", lambda _plan_id: ("plan text", "captured"))
    monkeypatch.setattr(main, "parse_explain_analyze", lambda _text: tree)
    monkeypatch.setattr(main, "analyze_rules", lambda _tree: warnings)

    main.get_ai_insights("demo", mocker.Mock())

    fallback.assert_called_once_with("SELECT 1", warnings, tree_summary=tree)

def test_legacy_sql_rewrite_response_is_rejected(ai_setup, mocker):
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query reads orders.",
        "suggestion": "",
        "suggested_query": {
            "sql": "DELETE FROM orders",
            "why_it_may_help": "It removes data.",
            "what_changed": "Changed the statement type.",
            "limitations": "None.",
        },
    }))

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert result["ai_status"] == "fallback"
    assert "suggested_query" not in result
    assert "fix" not in result
    assert "sql" not in result["suggestion"].lower()

def test_sql_code_in_ai_suggestion_is_replaced_with_plain_english_fallback(ai_setup, mocker):
    ai_setup.return_value = _response(mocker, text=json.dumps({
        "summary": "This query reads orders.",
        "finding": "The plan does not show a clear performance concern.",
        "suggestion": "Try this: SELECT id FROM orders WHERE amount > 500",
    }))

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert result["ai_status"] == "success"
    assert result["suggestion"] == ai._NO_CHANGE
    assert "SELECT" not in result["suggestion"]

def test_truncated_plan_and_warning_evidence_is_marked_for_gemini(ai_setup, mocker):
    root = {
        "id": "root",
        "operator": "Filter",
        "details": "(orders.amount > 500) " + ("detail " * 100),
        "children": [
            {
                "id": f"child-{index}",
                "operator": "Table scan",
                "details": "on orders",
                "estimated_rows": 5000,
                "actual_rows": 5000,
                "rows_total": 5000,
                "loops": 1,
                "time_total_ms": 30,
                "self_time_ms": 30,
                "children": [],
            }
            for index in range(21)
        ],
    }
    warnings = [dict(WARNINGS[0]) for _ in range(21)]
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation(QUERY, warnings, tree_summary=root)

    prompt = ai_setup.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
    data = json.loads(prompt.split("DATA:\n", 1)[1])
    assert data["parsed_plan"]["nodes_truncated"] is True
    assert data["warnings_truncated"] is True
    assert data["evidence_notice"]
    assert "omitted evidence may change the interpretation" in data["evidence_notice"].lower()
    assert len(data["parsed_plan"]["nodes"][0]["details"]) == 500
    assert data["parsed_plan"]["nodes_truncated"] is True
    assert data["parsed_plan"]["complete"] is False
    assert data["warnings_complete"] is False
    assert data["evidence_complete"] is False
    assert "never call the supplied plan or warning list complete" in prompt.lower()

def test_clipped_plan_node_details_are_marked_incomplete(ai_setup, mocker):
    tree = {
        "id": "node-1",
        "operator": "Table scan",
        "details": "on orders " + ("detail " * 100),
        "actual_rows": 10,
        "rows_total": 10,
        "loops": 1,
        "children": [],
    }
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation("SELECT id FROM orders", [], tree_summary=tree)

    data = _submitted_ai_context(ai_setup)
    node = data["parsed_plan"]["nodes"][0]
    assert len(node["details"]) == 500
    assert node["details_truncated"] is True
    assert node["node_fields_truncated"] is True
    assert data["parsed_plan"]["node_details_truncated"] is True
    assert data["parsed_plan"]["complete"] is False
    assert data["evidence_complete"] is False
    assert "node details were clipped" in data["evidence_notice"]

def test_clipped_warning_fields_are_marked_incomplete(ai_setup, mocker):
    tree = {
        "id": "node-1",
        "operator": "Table scan",
        "details": "on " + ("x" * 600),
        "actual_rows": 5000,
        "rows_total": 5000,
        "loops": 1,
        "self_time_ms": 30,
        "children": [],
    }
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation("SELECT * FROM orders", [], tree_summary=tree)

    data = _submitted_ai_context(ai_setup)
    assert data["warning_fields_truncated"] is True
    assert data["warnings_complete"] is False
    assert "evidence" in data["warnings"][0]["fields_truncated"]
    assert data["warnings"][0]["complete"] is False
    assert "warning fields were clipped" in data["evidence_notice"]

def test_truncated_schema_table_list_is_not_marked_complete(ai_setup, mocker):
    schema_info = {"tables": [
        {
            "schema": "test_db", "table": f"table_{index}", "columns": ["id"],
            "columns_complete": True, "indexes": [], "index_catalog_available": True,
        }
        for index in range(21)
    ]}
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation("SELECT 1", [], tree_summary=TREE, schema_info=schema_info)

    data = _submitted_ai_context(ai_setup)
    assert len(data["schema_info"]["tables"]) == 20
    assert data["schema_info"]["tables_truncated"] is True
    assert data["schema_info"]["tables_complete"] is False
    assert data["schema_info"]["complete_index_catalog_available"] is False
    assert data["evidence_complete"] is False
    assert "schema table list was limited" in data["evidence_notice"]

def test_truncated_index_list_is_not_marked_complete(ai_setup, mocker):
    schema_info = {"tables": [{
        "schema": "test_db", "table": "orders", "columns": ["id"],
        "columns_complete": True,
        "indexes": [
            {"name": f"idx_{index}", "columns": ["id"], "unique": False}
            for index in range(31)
        ],
        "index_catalog_available": True,
    }]}
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation("SELECT id FROM orders", [], tree_summary=TREE, schema_info=schema_info)

    data = _submitted_ai_context(ai_setup)
    table = data["schema_info"]["tables"][0]
    assert len(table["indexes"]) == 30
    assert table["indexes_truncated"] is True
    assert table["indexes_complete"] is False
    assert table["index_catalog_available"] is False
    assert data["schema_info"]["complete_index_catalog_available"] is False
    assert "schema index lists were limited" in data["evidence_notice"]

def test_truncated_index_column_list_is_not_marked_complete(ai_setup, mocker):
    schema_info = {"tables": [{
        "schema": "test_db", "table": "orders", "columns": ["id"],
        "columns_complete": True,
        "indexes": [{"name": "wide_idx", "columns": [f"col_{i}" for i in range(21)]}],
        "index_catalog_available": True,
    }]}
    ai_setup.return_value = _response(mocker)

    ai.get_ai_explanation("SELECT id FROM orders", [], tree_summary=TREE, schema_info=schema_info)

    data = _submitted_ai_context(ai_setup)
    table = data["schema_info"]["tables"][0]
    assert len(table["indexes"][0]["columns"]) == 20
    assert table["index_columns_truncated"] is True
    assert table["indexes_complete"] is False
    assert data["schema_info"]["complete_index_catalog_available"] is False
    assert "schema index lists were limited" in data["evidence_notice"]
