import pytest

from security import UnsafeQueryError, validate_query

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
