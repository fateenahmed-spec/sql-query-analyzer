import json
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
from sqlparse.sql import Identifier, IdentifierList, Parenthesis
from dotenv import load_dotenv

from security import validate_live_query, validate_query

load_dotenv(Path(__file__).resolve().with_name(".env"))

class UnsupportedServerError(Exception):
    pass

class DatabaseUploadConfigurationError(RuntimeError):
    pass

def get_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST", "127.0.0.1"), user=os.getenv("DB_USER", "analyzer_ro"), password=os.getenv("DB_PASS", ""), database=os.getenv("DB_NAME", "sql_analyzer_test"))


UPLOAD_SCHEMA = "analyzer_uploads"


_UPLOAD_BLOCKED_PATTERNS = (
    re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
    re.compile(r"\bGRANT\b", re.IGNORECASE),
    re.compile(r"\bCREATE\s+USER\b", re.IGNORECASE),
    re.compile(r"\bLOAD\s+DATA\b", re.IGNORECASE),
    re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE),
)


def _validate_upload_statement(statement: str) -> None:
    upper_statement = statement.strip().upper()
    # Check both raw SQL and comment-stripped SQL. The raw check catches
    # executable MySQL comments; the normalized check catches comments used
    # to split otherwise contiguous blocked keywords.
    normalized_statement = sqlparse.format(
        upper_statement,
        strip_comments=True,
        reindent=False,
    )
    candidates = (upper_statement, " ".join(normalized_statement.split()))
    for pattern in _UPLOAD_BLOCKED_PATTERNS:
        if any(pattern.search(candidate) for candidate in candidates):
            raise ValueError(f"Disallowed SQL command: {pattern.pattern}")


def execute_sql_file(sql_text: str) -> str:
    """Execute statements from an uploaded SQL script using the configured admin account."""
    statements = sqlparse.split(sql_text.lstrip("\ufeff"))
    if not any(statement.strip() for statement in statements):
        raise ValueError("The SQL file contains no statements.")
    for i, statement in enumerate(statements):
        statement = statement.strip()
        if not statement:
            continue
        try:
            _validate_upload_statement(statement)
        except Exception as exc:
            raise ValueError(
                f"Failed at statement {i + 1}: {statement[:80]}... Error: {exc}"
            ) from exc

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
        with closing(conn.cursor(buffered=True)) as cursor:
            for i, statement in enumerate(statements):
                statement = statement.strip()
                if not statement:
                    continue
                try:
                    cursor.execute(statement)
                    # Drain result rows before issuing the next statement.
                    if cursor.with_rows:
                        cursor.fetchall()
                except Exception as exc:
                    conn.rollback()
                    raise Exception(
                        f"Failed at statement {i + 1}: {statement[:80]}... Error: {exc}"
                    ) from exc
        conn.commit()
    finally:
        conn.close()

    return f"Executed {len(statements)} SQL statements successfully."

def kill_query(conn_id, fired_event):
    conn = None
    try:
        conn = get_connection()
        with closing(conn.cursor()) as cursor:
            try:
                cursor.execute(f"KILL QUERY {int(conn_id)}")
            except mysql.connector.Error:
                # The target query may finish between the timer firing and KILL.
                pass
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
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                if fired_event.is_set() or elapsed_ms >= 5000:
                    raise TimeoutError("Query exceeded 5s limit")
                return {"raw_plan": "\n".join(str(row[0]) for row in rows), "mysql_version": version, "elapsed_ms": elapsed_ms}
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


def _bare_column_name(label) -> str:
    """Return the final identifier from a bare or schema-qualified column label."""
    text = str(label).strip()
    parts = []
    current = []
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "`":
            if quoted and index + 1 < len(text) and text[index + 1] == "`":
                current.append("`")
                index += 2
                continue
            quoted = not quoted
        elif char == "." and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts[-1].strip()


_TABLE_ACCESS = re.compile(
    r"\b(?:Table scan|Index scan|Index range scan|Index lookup|"
    r"Single-row index lookup|Covering index lookup|Covering index scan|"
    r"Single-row covering index lookup)\s+on\s+"
    r"(?:`?([\w$]+)`?\.)?`?([\w$]+)`?",
    re.IGNORECASE,
)

_QUERY_SOURCE = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|[\w$]+)(?:\.(?:`[^`]+`|[\w$]+))?)"
    r"(?:\s+(?:AS\s+)?(`?[\w$]+`?))?",
    re.IGNORECASE,
)
_SQL_RESERVED = {
    "where", "join", "inner", "left", "right", "cross", "on", "group", "order",
    "having", "limit", "union", "offset", "using", "set", "for", "lock",
}


from row_flow import build_row_flow


def _query_cte_names(query_text: str) -> set[str]:
    statements = sqlparse.parse(query_text or "")
    if not statements:
        return set()
    tokens = [token for token in statements[0].tokens if not token.is_whitespace]
    if not tokens or tokens[0].value.upper() != "WITH":
        return set()
    index = 1
    if index < len(tokens) and tokens[index].value.upper() == "RECURSIVE":
        index += 1
    if index >= len(tokens):
        return set()
    token = tokens[index]
    if isinstance(token, IdentifierList):
        definitions = token.get_identifiers()
    elif isinstance(token, Identifier):
        definitions = [token]
    else:
        return set()
    return {
        name.casefold()
        for definition in definitions
        if (name := definition.get_name())
    }


def _query_table_aliases(query_text: str) -> dict[str, tuple[str | None, str, str]]:
    query = sqlparse.format(query_text or "", strip_comments=True, reindent=False)
    cte_names = _query_cte_names(query)
    aliases = {}
    for match in _QUERY_SOURCE.finditer(query):
        source = match.group(1)
        parts = [quoted or bare for quoted, bare in re.findall(r"`([^`]+)`|([\w$]+)", source)]
        if not parts:
            continue
        schema = parts[-2] if len(parts) > 1 else None
        table = parts[-1]
        if table.casefold() in cte_names:
            continue
        alias = (match.group(2) or "").strip("`")
        if not alias or alias.casefold() in _SQL_RESERVED:
            continue
        aliases[alias.casefold()] = (schema, table, alias)
    return aliases


def _query_derived_references(query_text: str) -> set[str]:
    query = sqlparse.format(query_text or "", strip_comments=True, reindent=False)
    derived = _query_cte_names(query)
    for match in _QUERY_SOURCE.finditer(query):
        source = match.group(1)
        parts = [quoted or bare for quoted, bare in re.findall(r"`([^`]+)`|([\w$]+)", source)]
        if not parts or parts[-1].casefold() not in derived:
            continue
        derived.add(parts[-1].casefold())
        alias = (match.group(2) or "").strip("`")
        if alias and alias.casefold() not in _SQL_RESERVED:
            derived.add(alias.casefold())

    def collect_derived_aliases(token):
        if isinstance(token, Identifier) and any(isinstance(child, Parenthesis) for child in token.tokens):
            alias = token.get_alias()
            if alias:
                derived.add(alias.casefold())
        for child in getattr(token, "tokens", []):
            collect_derived_aliases(child)

    for statement in sqlparse.parse(query):
        collect_derived_aliases(statement)
    return derived


def _source_table_names(plan_text: str, cursor, query_text: str = "") -> list[tuple[str, str, str | None]]:
    cursor.execute("SELECT DATABASE()")
    default_schema_row = cursor.fetchone()
    default_schema = str(default_schema_row[0]) if default_schema_row else "sql_analyzer_test"
    aliases = _query_table_aliases(query_text)
    derived_references = _query_derived_references(query_text)
    candidates = []
    seen_references = set()
    for match in _TABLE_ACCESS.finditer(plan_text):
        plan_schema = match.group(1).strip("`") if match.group(1) else None
        reference = match.group(2)
        source = aliases.get(reference.casefold())
        if source is None and reference.casefold() in derived_references:
            continue
        reference_identity = (reference.casefold(), source[2].casefold() if source else None)
        if reference_identity in seen_references:
            continue
        seen_references.add(reference_identity)
        if source:
            schema_hint, table_name, alias = source
            schemas = [schema_hint] if schema_hint else [default_schema, UPLOAD_SCHEMA]
        else:
            table_name = reference
            alias = None
            schemas = [plan_schema] if plan_schema else [default_schema, UPLOAD_SCHEMA]
        for schema in schemas:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s LIMIT 1",
                (schema, table_name),
            )
            table_row = cursor.fetchone()
            if table_row:
                resolved_table = str(table_row[0])
                candidates.append((schema, resolved_table, alias))
                break
    return candidates


def run_query_with_data(sql: str) -> dict:
    """Validate and execute one read-only query, returning its real plan and rows."""
    normalized_sql = validate_live_query(sql).strip().rstrip(";").rstrip()
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
        columns = [_bare_column_name(column[0]) for column in (cursor.description or [])]
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
            table_names = _source_table_names(plan_text, cursor, normalized_sql)
            for schema_name, table_name, alias in table_names:
                escaped_schema = schema_name.replace("`", "``")
                escaped_table = table_name.replace("`", "``")
                cursor.execute(f"SELECT * FROM `{escaped_schema}`.`{escaped_table}` LIMIT 10")
                table_columns = [_bare_column_name(column[0]) for column in (cursor.description or [])]
                if alias:
                    sample_key = f"{alias} ({schema_name}.{table_name})"
                else:
                    sample_key = table_name if schema_name == (os.getenv("DB_NAME") or "sql_analyzer_test") else f"{schema_name}.{table_name}"
                source_samples[sample_key] = [
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
