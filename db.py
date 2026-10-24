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


_TABLE_ACCESS = re.compile(
    r"\b(?:Table scan|Index scan|Index range scan|Index lookup|"
    r"Single-row index lookup|Covering index lookup|Covering index scan|"
    r"Single-row covering index lookup)\s+on\s+"
    r"(?:`?([\w$]+)`?\.)?`?([\w$]+)`?",
    re.IGNORECASE,
)

_FLOW_SCAN_OPERATORS = {
    "Table scan",
    "Index scan",
    "Index range scan",
    "Index lookup",
    "Single-row index lookup",
    "Covering index lookup",
    "Covering index scan",
    "Single-row covering index lookup",
}
_FLOW_FORBIDDEN_EXPRESSION = re.compile(
    r";|--|#|/\*|\*/|\b(?:SELECT|FROM|UNION|WITH|EXISTS|INTO|OUTFILE|LOAD_FILE)\b",
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


def _flow_action(operator: str) -> str:
    if operator in _FLOW_SCAN_OPERATORS:
        return "scan"
    if operator == "Filter":
        return "filter"
    if "join" in operator.casefold():
        return "join"
    if operator.startswith("Aggregate") or operator in {"Group aggregate", "GroupAggregate"}:
        return "aggregate"
    if operator == "Sort":
        return "sort"
    if operator == "Limit":
        return "limit"
    return "other"


def _flow_nodes_postorder(root: dict) -> list[dict]:
    result = []

    def visit(node):
        for child in node.get("children", []):
            visit(child)
        result.append(node)

    visit(root)
    return result


def _flow_aliases(query_text: str) -> dict[str, tuple[str, str]]:
    aliases = {}
    for match in _QUERY_SOURCE.finditer(query_text or ""):
        source = match.group(1)
        alias = match.group(2) or ""
        parts = [part.strip("`") for part in source.split(".")]
        table = parts[-1]
        alias = alias.strip("`")
        if alias.casefold() in _SQL_RESERVED:
            alias = ""
        if len(parts) > 1:
            aliases[table.casefold()] = ("", table)
            if alias:
                aliases[alias.casefold()] = ("", alias)
            continue
        aliases[table.casefold()] = (table, table)
        if alias:
            aliases[alias.casefold()] = (table, alias)
    return aliases


def _flow_table_ref(node: dict, aliases: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    match = re.match(r"\s*on\s+(?:`?([\w$]+)`?\.)?`?([\w$<>]+)`?", str(node.get("details") or ""), re.IGNORECASE)
    if not match:
        return None
    reference = match.group(2)
    if reference.startswith("<"):
        return None
    return aliases.get(reference.casefold(), (reference, reference))


def _flow_table_for_node(node: dict, aliases: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    if node.get("operator") in _FLOW_SCAN_OPERATORS:
        return _flow_table_ref(node, aliases)
    for child in node.get("children", []):
        found = _flow_table_for_node(child, aliases)
        if found:
            return found
    return None


def _safe_flow_expression(expression: str) -> bool:
    return bool(expression.strip()) and not _FLOW_FORBIDDEN_EXPRESSION.search(expression)


def _flow_pk_where(pk_columns: list[str], pk_values: list[tuple], alias: str = "") -> tuple[str, list]:
    prefix = f"{_quote_identifier(alias)}." if alias else ""
    if len(pk_columns) == 1:
        column = f"{prefix}{_quote_identifier(pk_columns[0])}"
        placeholders = ", ".join(["%s"] * len(pk_values))
        return f"{column} IN ({placeholders})", [values[0] for values in pk_values]

    groups = []
    parameters = []
    for values in pk_values:
        groups.append("(" + " AND ".join(
            f"{prefix}{_quote_identifier(column)} = %s" for column in pk_columns
        ) + ")")
        parameters.extend(values)
    return "(" + " OR ".join(groups) + ")", parameters


def _quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _flow_row_count(cursor, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def _flow_table_metadata(cursor, table_ref: tuple[str, str], cache: dict) -> dict | None:
    requested_table, alias = table_ref
    cache_key = requested_table.casefold()
    if cache_key in cache:
        metadata = cache[cache_key]
        return {**metadata, "alias": alias or metadata["table"]} if metadata else None

    cursor.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1",
        (requested_table,),
    )
    table_row = cursor.fetchone()
    if not table_row:
        cache[cache_key] = None
        return None
    table = str(table_row[0])
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
        (table,),
    )
    pk_columns = [str(row[0]) for row in cursor.fetchall()]
    if not pk_columns:
        cache[cache_key] = None
        return None

    cursor.execute(
        f"SELECT * FROM {_quote_identifier(table)} "
        "ORDER BY " + ", ".join(_quote_identifier(column) for column in pk_columns) + " LIMIT 10"
    )
    columns = [column[0] for column in (cursor.description or [])]
    values = cursor.fetchall()
    sample = []
    for row in values:
        row_values = {column: _json_safe(value) for column, value in zip(columns, row)}
        pk_values = tuple(row_values[column] for column in pk_columns)
        pk_label = str(pk_values[0]) if len(pk_values) == 1 else json.dumps(pk_values, ensure_ascii=False, separators=(",", ":"), default=str)
        rid = f"{table}:{pk_label}"
        sample.append({"rid": rid, "pk_values": pk_values, "values": row_values})

    metadata = {
        "table": table,
        "alias": alias or table,
        "pk_columns": pk_columns,
        "sample": sample,
        "table_row_count": _flow_row_count(cursor, table),
    }
    cache[cache_key] = metadata
    return metadata


def _flow_select_rids(cursor, metadata: dict, records: dict, pk_by_rid: dict, rids: list[str], order_by: str | None = None, limit: int | None = None) -> list[str]:
    selected = [rid for rid in rids if rid in records]
    if not selected:
        return []
    pk_columns = metadata["pk_columns"]
    if len(pk_columns) > 1 and order_by and not re.fullmatch(
        r"[\w$`.,\s]+(?:\s+(?:ASC|DESC))?(?:\s*,\s*[\w$`.,\s]+(?:\s+(?:ASC|DESC))?)*",
        order_by,
        re.IGNORECASE,
    ):
        return []
    pk_values = [pk_by_rid[rid]["pk_values"] for rid in selected]
    where, parameters = _flow_pk_where(pk_columns, pk_values)
    sql = (
        "SELECT " + ", ".join(_quote_identifier(column) for column in pk_columns)
        + f" FROM {_quote_identifier(metadata['table'])} AS {_quote_identifier(metadata['alias'])}"
        + f" WHERE {where}"
    )
    if order_by:
        sql += f" ORDER BY {order_by}"
    elif len(pk_columns) == 1:
        column = f"{_quote_identifier(metadata['alias'])}.{_quote_identifier(pk_columns[0])}"
        sql += f" ORDER BY FIELD({column}, " + ", ".join(["%s"] * len(pk_values)) + ")"
        parameters.extend(values[0] for values in pk_values)
    else:
        return []
    if limit is not None:
        sql += " LIMIT %s"
        parameters.append(limit)
    cursor.execute(sql, tuple(parameters))
    ordered_keys = cursor.fetchall()
    rid_for_pk = {
        pk_by_rid[rid]["pk_values"]: rid
        for rid, record in records.items()
        if record["table"] == metadata["table"] and rid in pk_by_rid
    }
    return [rid_for_pk[tuple(row)] for row in ordered_keys if tuple(row) in rid_for_pk]


def build_row_flow(plan_tree: dict, conn) -> list[dict]:
    """Build a deterministic, sample-based row flow using live rows and SQL predicates."""
    query_text = str(plan_tree.get("_flow_query") or "")
    aliases = _flow_aliases(query_text)
    metadata_cache = {}
    records = {}
    pk_by_rid = {}
    current_rids: list[str] = []
    steps = []

    with closing(conn.cursor()) as cursor:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
        for node in _flow_nodes_postorder(plan_tree):
            operator = str(node.get("operator") or "Other")
            action = _flow_action(operator)
            input_rids = list(current_rids)
            actual_rows = node.get("rows_total")
            if actual_rows is None:
                actual_rows = node.get("actual_rows")
            loops = node.get("loops") or 1
            estimate = node.get("estimated_rows")
            est_rows = float(estimate or 0) * float(loops)
            table_ref = _flow_table_for_node(node, aliases)
            metadata = _flow_table_metadata(cursor, table_ref, metadata_cache) if table_ref else None
            row_results = {}
            evaluable = False
            table_row_count = metadata["table_row_count"] if metadata else 0
            note = ""
            output_rids = []
            row_ids = input_rids

            never_executed = bool(node.get("never_executed"))
            if action == "join" and not input_rids and float(actual_rows or 0) == 0:
                never_executed = True

            if never_executed:
                note = "Never executed: upstream returned 0 rows."
                evaluable = False
                row_results = {rid: False for rid in input_rids}
            elif action == "scan" and metadata:
                sample = metadata["sample"]
                for item in sample:
                    rid = item["rid"]
                    record = {
                        "table": metadata["table"],
                        "values": item["values"],
                        "table_row_count": metadata["table_row_count"],
                    }
                    records[rid] = record
                    pk_by_rid[rid] = {"pk_columns": metadata["pk_columns"], "pk_values": item["pk_values"]}
                output_rids = [item["rid"] for item in sample]
                row_ids = output_rids
                row_results = {rid: True for rid in output_rids}
                evaluable = True
                note = f"Sampled {len(output_rids)} real rows from {metadata['table']} in primary-key order."
            elif action == "filter" and metadata and _safe_flow_expression(str(node.get("details") or "")):
                condition = str(node.get("details") or "").strip()
                if condition.startswith("(") and condition.endswith(")"):
                    condition = condition[1:-1].strip()
                if input_rids and all(rid in records and records[rid]["table"] == metadata["table"] for rid in input_rids):
                    pk_values = [pk_by_rid[rid]["pk_values"] for rid in input_rids]
                    where, parameters = _flow_pk_where(metadata["pk_columns"], pk_values, metadata["alias"])
                    qualified_refs = re.findall(r"(?<![\w$\.\d])([A-Za-z_$][\w$]*)\s*\.", condition)
                    allowed_qualifiers = {metadata["alias"].casefold(), metadata["table"].casefold()}
                    if all(ref.casefold() in allowed_qualifiers for ref in qualified_refs):
                        query = (
                            "SELECT " + ", ".join(f"{_quote_identifier(metadata['alias'])}.{_quote_identifier(column)}" for column in metadata["pk_columns"])
                            + f", ({condition}) AS passed FROM {_quote_identifier(metadata['table'])} AS {_quote_identifier(metadata['alias'])}"
                            + f" WHERE {where}"
                        )
                        cursor.execute(query, tuple(parameters))
                        for result in cursor.fetchall():
                            key = tuple(result[:len(metadata["pk_columns"])])
                            rid = next((candidate for candidate in input_rids if pk_by_rid[candidate]["pk_values"] == key), None)
                            if rid is not None:
                                row_results[rid] = bool(result[-1])
                        output_rids = [rid for rid in input_rids if row_results.get(rid, False)]
                        evaluable = True
                        note = f"Evaluated the real predicate against {len(input_rids)} sample rows in MySQL."
                if not evaluable:
                    note = "Predicate could not be safely evaluated on this sample."
            elif action == "sort" and metadata and input_rids:
                order_by = str(node.get("details") or "").strip()
                order_by = re.sub(r",\s*limit input to\b.*$", "", order_by, flags=re.IGNORECASE).strip()
                limit_match = re.search(r"limit input to\s+(\d+)\s+row", str(node.get("details") or ""), re.IGNORECASE)
                safe_order = _safe_flow_expression(order_by) and bool(
                    re.fullmatch(r"[\w$`.,\s]+(?:\s+(?:ASC|DESC))?(?:\s*,\s*[\w$`.,\s]+(?:\s+(?:ASC|DESC))?)*", order_by, re.IGNORECASE)
                )
                if safe_order and all(rid in records and records[rid]["table"] == metadata["table"] for rid in input_rids):
                    output_rids = _flow_select_rids(cursor, metadata, records, pk_by_rid, input_rids, order_by=order_by, limit=int(limit_match.group(1)) if limit_match else None)
                    evaluable = bool(output_rids) or not input_rids
                    if evaluable:
                        row_results = {rid: True for rid in input_rids}
                        note = "Applied the real ORDER BY to the sample rows in MySQL."
                if not evaluable:
                    note = "Sort expression could not be safely evaluated on this sample."
            elif action == "limit" and metadata and input_rids:
                limit_match = re.search(r"(?:limit\s*:\s*|^\s*)(\d+)\s+row", str(node.get("details") or ""), re.IGNORECASE)
                if limit_match and all(rid in records and records[rid]["table"] == metadata["table"] for rid in input_rids):
                    output_rids = _flow_select_rids(cursor, metadata, records, pk_by_rid, input_rids, limit=int(limit_match.group(1)))
                    evaluable = bool(output_rids) or not input_rids
                    if evaluable:
                        output_set = set(output_rids)
                        row_results = {rid: rid in output_set for rid in input_rids}
                        note = f"Applied LIMIT {limit_match.group(1)} to the sample rows in MySQL."
                if not evaluable:
                    note = "Limit could not be safely evaluated on this sample."
            else:
                note = f"{operator} is not evaluated on this sample; survivors are scaled from plan counts."

            if not evaluable and not never_executed:
                child_actual = sum(float(child.get("rows_total") or child.get("actual_rows") or 0) for child in node.get("children", []))
                output_actual = float(actual_rows or 0)
                survivor_count = min(len(input_rids), max(0, round(len(input_rids) * output_actual / child_actual))) if child_actual > 0 else 0
                output_rids = input_rids[:survivor_count]
                row_results = {rid: rid in set(output_rids) for rid in input_rids}
                note = f"Scaled from plan counts; selected the first {survivor_count} of {len(input_rids)} sample rows."

            row_data = {
                rid: records[rid]
                for rid in dict.fromkeys(input_rids + output_rids)
                if rid in records
            }
            step = {
                "node_id": str(node.get("id") or ""),
                "op": action,
                "label": f"{operator}: {node.get('details') or ''}".rstrip(": "),
                "condition": str(node.get("details") or "") if action == "filter" else "",
                "input_rids": input_rids,
                "output_rids": output_rids,
                "evaluable": evaluable,
                "est_rows": est_rows,
                "actual_rows": float(actual_rows or 0),
                "table_row_count": table_row_count,
                "never_executed": never_executed,
                "row_results": row_results,
                "note": note,
                "row_data": row_data,
            }
            steps.append(step)
            current_rids = list(output_rids)

    return steps


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
            table_names = _source_table_names(plan_text, cursor, normalized_sql)
            for schema_name, table_name, alias in table_names:
                escaped_schema = schema_name.replace("`", "``")
                escaped_table = table_name.replace("`", "``")
                cursor.execute(f"SELECT * FROM `{escaped_schema}`.`{escaped_table}` LIMIT 10")
                table_columns = [column[0] for column in (cursor.description or [])]
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
