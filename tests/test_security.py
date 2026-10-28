import pytest

from security import (
    UnsafeQueryError,
    resolve_uploaded_table_references,
    validate_live_query,
    validate_query,
)

@pytest.mark.parametrize("query", [
    "SELECT 1", "select 1", "SELECT\n1", "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT 'DELETE'", "SELECT 1 -- comment", "SELECT REPLACE(name,'a','b')",
    "SELECT 'a;b'", "SELECT 'it''s'", "EXPLAIN ANALYZE SELECT 1", "SELECT 1\n",
])
def test_safe(query):
    assert validate_query(query)

@pytest.mark.parametrize("query", [
    "DROP TABLE x", "SELECT 1; SELECT 2", "SELECT * INTO/**/OUTFILE 'x'", "SELECT * FROM x FOR/**/UPDATE",
    "SELECT * FROM x FOR SHARE", "SELECT * FROM x LOCK IN SHARE MODE", "SELECT SLEEP (5)",
    "SELECT 1--1+SLEEP(9)", "/*!50000 SLEEP(5)*/ SELECT 1", "SELECT /*+ hint */ 1",
    "REPLACE INTO x VALUES (1)", "sElEcT 1; dRoP TABLE x", "SELECT 1; SELECT 2",
    "SELECT 1 # comment", "", "-- comment only",
])
def test_unsafe(query):
    with pytest.raises(UnsafeQueryError):
        validate_query(query)


@pytest.mark.parametrize("query", [
    "SELECT * FROM analyzer_uploads.up_customers",
    "SELECT * FROM `analyzer_uploads`.`up_customers`",
    "SELECT * FROM customers",
    "SELECT * FROM sql_analyzer_test.customers",
])
def test_live_query_allows_app_schemas(query):
    assert validate_live_query(query)


@pytest.mark.parametrize("query", [
    "SELECT * FROM analyzer_uploads.customers",
    "SELECT * FROM other_schema.customers",
    "SELECT * FROM analyzer_uploads.customers JOIN analyzer_uploads.up_ok ON 1=1",
    "SELECT * FROM other_schema.customers",
    "SELECT * FROM sql_analyzer_test.customers, analyzer_uploads.customers",
    "SELECT * FROM (SELECT * FROM other_schema.customers) AS nested",
    "SELECT * FROM db.schema.table",
    "INSERT INTO analyzer_uploads.up_customers VALUES (1)",
    "UPDATE analyzer_uploads.up_customers SET id=1",
    "DELETE FROM analyzer_uploads.up_customers",
])
def test_live_query_rejects_other_schemas_and_writes(query):
    with pytest.raises(UnsafeQueryError):
        validate_live_query(query)


def test_uploaded_logical_name_resolves_for_simple_select():
    resolved = resolve_uploaded_table_references(
        "SELECT * FROM employees LIMIT 10",
        ["up_employees"],
        ["customers", "orders"],
    )

    assert resolved == "SELECT * FROM `analyzer_uploads`.`up_employees` LIMIT 10"


def test_uploaded_logical_names_resolve_for_joins_and_keep_aliases():
    resolved = resolve_uploaded_table_references(
        "SELECT e.id FROM employees AS e JOIN orders o ON e.id = o.employee_id",
        ["up_employees", "up_orders"],
        ["customers"],
    )

    assert resolved == (
        "SELECT e.id FROM `analyzer_uploads`.`up_employees` AS e "
        "JOIN `analyzer_uploads`.`up_orders` o ON e.id = o.employee_id"
    )


def test_quoted_table_name_resolves_without_changing_its_alias():
    resolved = resolve_uploaded_table_references(
        "SELECT e.id FROM `employees` AS e",
        ["up_employees"],
        [],
    )

    assert resolved == "SELECT e.id FROM `analyzer_uploads`.`up_employees` AS e"


def test_schema_qualified_tables_are_preserved():
    query = (
        "SELECT * FROM analyzer_uploads.up_employees "
        "JOIN sql_analyzer_test.orders ON 1 = 1"
    )

    assert resolve_uploaded_table_references(
        query,
        ["up_employees"],
        ["orders"],
    ) == query


def test_unknown_table_does_not_map_to_a_similarly_named_upload():
    query = "SELECT * FROM employee"

    assert resolve_uploaded_table_references(
        query,
        ["up_employees"],
        ["customers"],
    ) == query


def test_default_schema_table_stays_bare_when_no_upload_matches():
    query = "SELECT * FROM orders"

    assert resolve_uploaded_table_references(
        query,
        ["up_employees"],
        ["orders"],
    ) == query


def test_bare_name_matching_default_and_uploaded_table_is_ambiguous():
    with pytest.raises(UnsafeQueryError, match="Ambiguous table name 'orders'"):
        resolve_uploaded_table_references(
            "SELECT * FROM orders",
            ["up_orders"],
            ["orders"],
        )


def test_cte_names_are_not_resolved_but_uploaded_sources_in_ctes_are():
    resolved = resolve_uploaded_table_references(
        "WITH orders AS (SELECT * FROM employees) "
        "SELECT * FROM orders JOIN employees e ON 1 = 1",
        ["up_employees", "up_orders"],
        ["customers"],
    )

    assert resolved.count("`analyzer_uploads`.`up_employees`") == 2
    assert "FROM orders JOIN" in resolved


def test_nested_query_sources_are_resolved():
    resolved = resolve_uploaded_table_references(
        "SELECT * FROM (SELECT * FROM employees) AS e JOIN orders o ON 1 = 1",
        ["up_employees", "up_orders"],
        ["customers"],
    )

    assert resolved.count("`analyzer_uploads`.`up_employees`") == 1
    assert "JOIN `analyzer_uploads`.`up_orders` o" in resolved


def test_uploaded_name_resolution_keeps_read_only_validation():
    with pytest.raises(UnsafeQueryError):
        resolve_uploaded_table_references(
            "DELETE FROM employees",
            ["up_employees"],
            [],
        )
