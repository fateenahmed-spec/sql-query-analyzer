import os
import re
import threading
import time
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import mysql.connector
import sqlparse
from dotenv import load_dotenv

from security import validate_query

load_dotenv(Path(__file__).resolve().with_name(".env"))

class UnsupportedServerError(Exception):
    pass

class DatabaseUploadConfigurationError(RuntimeError):
    pass

def get_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST", "127.0.0.1"), user=os.getenv("DB_USER", "analyzer_ro"), password=os.getenv("DB_PASS", ""), database=os.getenv("DB_NAME", "sql_analyzer_test"))

def execute_sql_file(sql_text: str) -> str:
    """Execute statements from an uploaded SQL script using the configured admin account."""
    statements = [statement.strip() for statement in sqlparse.split(sql_text.lstrip("\ufeff")) if statement.strip()]
    if not statements:
        raise ValueError("The SQL file contains no statements.")

    root_password = os.getenv("MYSQL_ROOT_PASSWORD", "")
    if not root_password:
        raise DatabaseUploadConfigurationError(
            "SQL file uploads require MYSQL_ROOT_PASSWORD to be configured."
        )

    conn = mysql.connector.connect(
        host=os.getenv("DB_HOST") or "127.0.0.1",
        user=os.getenv("DB_ADMIN_USER") or "root",
        password=root_password,
        database=os.getenv("DB_NAME") or "sql_analyzer_test",
    )
    try:
        with closing(conn.cursor()) as cursor:
            for statement in statements:
                cursor.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return f"Executed {len(statements)} SQL statements successfully."

def kill_query(conn_id, fired_event):
    conn = None
    try:
        conn = get_connection()
        with closing(conn.cursor()) as cursor:
            cursor.execute(f"KILL QUERY {int(conn_id)}")
    finally:
        fired_event.set()
        if conn is not None:
            conn.close()

def run_explain_analyze(raw_sql: str) -> dict:
    normalized_sql = validate_query(raw_sql)
    with get_connection() as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
            if "mariadb" in version.lower():
                raise UnsupportedServerError()
            try:
                version_tuple = tuple(int(p) for p in version.split("-")[0].split(".")[:3])
            except ValueError:
                raise UnsupportedServerError()
            if version_tuple < (8, 0, 18):
                raise UnsupportedServerError()
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT CONNECTION_ID()")
            conn_id = cursor.fetchone()[0]
            cursor.execute("SET SESSION max_execution_time=5000")
            fired_event = threading.Event()
            timer = threading.Timer(5.1, kill_query, args=(conn_id, fired_event))
            timer.daemon = True
            timer.start()
            started = time.perf_counter()
            try:
                cursor.execute(f"EXPLAIN ANALYZE {normalized_sql}")
                rows = cursor.fetchall()
                return {"raw_plan": "\n".join(str(row[0]) for row in rows), "mysql_version": version, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
            except Exception as exc:
                if fired_event.is_set() or getattr(exc, "errno", None) in (3024, 1317):
                    raise TimeoutError("Query exceeded 5s limit") from exc
                raise
            finally:
                timer.cancel()
                cursor.execute("ROLLBACK")


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


_TABLE_ACCESS = re.compile(
    r"\b(?:Table scan|Index scan|Index range scan|Index lookup|"
    r"Single-row index lookup|Covering index lookup|Covering index scan|"
    r"Single-row covering index lookup)\s+on\s+"
    r"(?:`?([\w$]+)`?\.)?`?([\w$]+)`?",
    re.IGNORECASE,
)


def _source_table_names(plan_text: str, cursor) -> list[str]:
    cursor.execute("SHOW TABLES")
    existing = {str(row[0]) for row in cursor.fetchall()}
    candidates = []
    for match in _TABLE_ACCESS.finditer(plan_text):
        table_name = match.group(2)
        if table_name in existing and table_name not in candidates:
            candidates.append(table_name)
    return candidates


def run_query_with_data(sql: str) -> dict:
    """Validate and execute one read-only query, returning its real plan and rows."""
    normalized_sql = validate_query(sql).strip().rstrip(";").rstrip()
    plan_text = ""
    rows = []
    columns = []

    conn = get_connection()
    try:
        with closing(conn.cursor()) as cursor:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
            cursor.execute(f"EXPLAIN ANALYZE {normalized_sql}")
            plan_text = "\n".join(str(row[0]) for row in cursor.fetchall())
    finally:
        conn.close()

    # Close this connection immediately after fetchmany so an unbuffered cursor
    # does not consume and retain the rest of a potentially large result set.
    data_conn = get_connection()
    try:
        cursor = data_conn.cursor()
        cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
        cursor.execute(normalized_sql)
        columns = [column[0] for column in (cursor.description or [])]
        rows = [
            {column: _json_safe(value) for column, value in zip(columns, row)}
            for row in cursor.fetchmany(20)
        ]
    finally:
        data_conn.close()

    source_samples = {}
    sample_conn = get_connection()
    try:
        with closing(sample_conn.cursor()) as cursor:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
            table_names = _source_table_names(plan_text, cursor)
            for table_name in table_names:
                escaped_table = table_name.replace("`", "``")
                cursor.execute(f"SELECT * FROM `{escaped_table}` LIMIT 10")
                table_columns = [column[0] for column in (cursor.description or [])]
                source_samples[table_name] = [
                    {
                        column: _json_safe(value)
                        for column, value in zip(table_columns, row)
                    }
                    for row in cursor.fetchmany(10)
                ]
    finally:
        sample_conn.close()

    return {
        "plan_text": plan_text,
        "rows": rows,
        "columns": columns,
        "source_samples": source_samples,
    }
