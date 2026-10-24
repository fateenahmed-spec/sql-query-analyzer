import mysql.connector
import pytest

from db import get_connection, run_explain_analyze


def _connection():
    return get_connection()


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
        run_explain_analyze("SELECT SUM(a.id + b.id + c.id) FROM orders a CROSS JOIN orders b CROSS JOIN orders c")
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
    grants = [row[0].upper() for row in cur.fetchall()]
    joined_grants = " ".join(grants)
    assert "USAGE" in joined_grants or "SELECT" in joined_grants
    assert "ALL PRIVILEGES" not in joined_grants
    assert any("SELECT ON `SQL_ANALYZER_TEST`.*" in grant for grant in grants)
    assert any(all(privilege in grant for privilege in ("SELECT", "INSERT", "CREATE", "DROP")) and "`ANALYZER_UPLOADS`.*" in grant for grant in grants)
    assert "INSERT ON `SQL_ANALYZER_TEST`" not in joined_grants
    assert "CREATE ON `SQL_ANALYZER_TEST`" not in joined_grants
    cur.close(); conn.close()
