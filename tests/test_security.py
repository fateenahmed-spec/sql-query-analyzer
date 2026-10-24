import pytest

from security import UnsafeQueryError, validate_live_query, validate_query

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
