import os

import mysql.connector
import pytest

from db import run_explain_analyze


def _connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"), user=os.getenv("DB_USER", "analyzer_ro"),
        password=os.getenv("DB_PASS", ""), database=os.getenv("DB_NAME", "sql_analyzer_test"),
    )


@pytest.fixture(scope="module")
def db_available():
    try:
        conn = _connection()
        conn.close()
    except mysql.connector.Error as exc:
        if exc.errno in (2002, 2003):
            pytest.skip("MySQL is unavailable")
        raise


def test_valid_query_returns_plan(db_available):
    result = run_explain_analyze("SELECT * FROM orders WHERE customer_id = 7")
    assert result["raw_plan"]
    assert result["mysql_version"]
    assert result["elapsed_ms"] >= 0


def test_cross_join_times_out_under_eight_seconds(db_available):
    import time
    from db import UnsupportedServerError
    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        run_explain_analyze("SELECT COUNT(*) FROM orders a CROSS JOIN orders b CROSS JOIN orders c")
    assert time.perf_counter() - started < 8


def test_user_cannot_write(db_available):
    conn = _connection()
    cur = conn.cursor()
    with pytest.raises(mysql.connector.Error):
        cur.execute("INSERT INTO customers (id) VALUES (999999)")
    with pytest.raises(mysql.connector.Error):
        cur.execute("CREATE TABLE forbidden (id INT)")
    conn.rollback(); cur.close(); conn.close()


def test_grants_are_read_only(db_available):
    conn = _connection()
    cur = conn.cursor()
    cur.execute("SHOW GRANTS")
    grants = " ".join(row[0].upper() for row in cur.fetchall())
    assert "USAGE" in grants or "SELECT" in grants
    assert "ALL PRIVILEGES" not in grants
    assert "INSERT" not in grants and "CREATE" not in grants
    cur.close(); conn.close()
