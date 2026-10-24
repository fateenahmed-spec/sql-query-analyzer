"""Build row-level animation records from live query-plan samples."""

from __future__ import annotations

import json
import re
from contextlib import closing
from decimal import Decimal

import mysql.connector


_SCAN_OPERATORS = {
    "Table scan", "Index scan", "Index range scan", "Index lookup",
    "Single-row index lookup", "Covering index lookup", "Covering index scan",
    "Single-row covering index lookup",
}
_RESERVED = {
    "where", "join", "inner", "left", "right", "cross", "on", "group", "order",
    "having", "limit", "union", "offset", "using", "set", "for", "lock",
}
_FORBIDDEN = re.compile(r";|--|#|/\*|\*/|\b(?:SELECT|FROM|UNION|WITH|EXISTS|INTO|OUTFILE|LOAD_FILE)\b", re.I)
_SOURCE = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|[\w$]+)(?:\.(?:`[^`]+`|[\w$]+))?)"
    r"(?:\s+(?:AS\s+)?(`?[\w$]+`?))?",
    re.I,
)
_JOIN_REF = r"(?:`(?:``|[^`])+`|[\w$]+)"
_JOIN_EQUALITY = re.compile(
    rf"(?P<left>{_JOIN_REF}(?:\s*\.\s*{_JOIN_REF})+)\s*=\s*"
    rf"(?P<right>{_JOIN_REF}(?:\s*\.\s*{_JOIN_REF})+)",
    re.I,
)
_TOKEN = re.compile(
    r"(?P<string>'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\")"
    r"|(?P<backtick>`(?:``|[^`])+`)|(?P<word>[A-Za-z_$][\w$]*)"
    r"|(?P<number>\d+(?:\.\d+)?)|(?P<space>\s+)|(?P<other>.)",
    re.S,
)
_KEYWORDS = {
    "and", "or", "not", "is", "null", "true", "false", "unknown", "like", "regexp", "rlike",
    "between", "in", "div", "mod", "xor", "case", "when", "then", "else", "end", "binary",
    "collate", "day", "month", "year", "hour", "minute", "second", "date", "time", "timestamp",
    "signed", "unsigned", "decimal", "char", "integer", "json", "escape", "sounds", "match",
    "against", "using", "charset", "current_date", "current_time", "current_timestamp",
}


def _ident(value: str) -> str:
    return value.strip().strip("`").replace("``", "`")


def _quote(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _bare_column_name(label) -> str:
    text = str(label).strip()
    parts, current, quoted, index = [], [], False, 0
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


def _bare_row(row: dict) -> dict:
    return {_bare_column_name(key): value for key, value in row.items()}


def _action(operator: str) -> str:
    if operator in _SCAN_OPERATORS:
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


def _postorder(root: dict) -> list[dict]:
    result = []

    def visit(node):
        for child in node.get("children", []):
            visit(child)
        result.append(node)

    visit(root)
    return result


def _aliases(query: str) -> dict[str, tuple[str | None, str, str]]:
    result = {}
    for match in _SOURCE.finditer(query or ""):
        parts = [_ident(part) for part in match.group(1).split(".")]
        if not parts:
            continue
        schema = parts[-2] if len(parts) > 1 else None
        table = parts[-1]
        alias = _ident(match.group(2) or "")
        if not alias or alias.casefold() in _RESERVED:
            alias = table
        result[alias.casefold()] = (schema, table, alias)
        result.setdefault(table.casefold(), (schema, table, table))
    return result


def _table_ref(node: dict, aliases: dict) -> tuple[str | None, str, str] | None:
    identifier = r"(?:`(?:``|[^`])+`|[\w$<>]+)"
    match = re.match(rf"\s*on\s+({identifier})(?:\s*\.\s*({identifier}))?", str(node.get("details") or ""), re.I)
    if not match:
        return None
    first = _ident(match.group(1))
    second = _ident(match.group(2) or "")
    plan_schema = first if second else None
    name = second or first
    if name.startswith("<"):
        return None
    found = aliases.get(name.casefold())
    if found:
        if plan_schema and found[0] and found[0].casefold() != plan_schema.casefold():
            return (plan_schema, found[1], found[2])
        return (plan_schema or found[0], found[1], found[2])
    return (plan_schema, name, name)


def _table_for_node(node: dict, aliases: dict):
    if node.get("operator") in _SCAN_OPERATORS:
        return _table_ref(node, aliases)
    for child in node.get("children", []):
        found = _table_for_node(child, aliases)
        if found:
            return found
    return None


def _identity(ref: tuple[str | None, str, str], default_schema: str):
    schema, table, alias = ref
    schema = schema or default_schema
    return schema.casefold(), table.casefold(), alias.casefold()


def _source_rows(samples: dict, schema: str, table: str, alias: str, default_schema: str):
    qualified = f"{schema}.{table}".casefold()
    identity_match = None
    table_match = None
    for key, rows in samples.items():
        text = str(key).replace("`", "").strip()
        alias_match = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", text)
        key_alias = alias_match.group(1).strip().casefold() if alias_match else ""
        key_name = alias_match.group(2).strip().casefold() if alias_match else text.casefold()
        if key_alias == alias.casefold() and key_name == qualified:
            return rows
        if key_name == qualified:
            identity_match = rows
        if key_name == table.casefold() and schema.casefold() == default_schema.casefold():
            table_match = rows
    return identity_match if identity_match is not None else table_match


def _rows_from_cursor(cursor, rows):
    columns = [_bare_column_name(column[0]) for column in (cursor.description or [])]
    return [
        {column: value.isoformat() if hasattr(value, "isoformat") else float(value) if isinstance(value, Decimal) else value for column, value in zip(columns, row)}
        for row in rows
    ]


def _safe_expression(expression: str) -> bool:
    return bool(expression.strip()) and not _FORBIDDEN.search(expression)


def _rewrite(expression: str, row: dict, metadata: dict, *, for_table: bool = False):
    tokens = [(match.lastgroup or "other", match.group(0)) for match in _TOKEN.finditer(expression)]
    columns = {str(key).casefold(): str(key) for key in row}
    allowed = {
        str(item).casefold()
        for item in (metadata.get("schema"), metadata.get("table"), metadata.get("alias"))
        if item
    }
    allowed_prefixes = {(item,) for item in allowed}
    if metadata.get("schema") and metadata.get("table"):
        allowed_prefixes.add((metadata["schema"].casefold(), metadata["table"].casefold()))
        allowed_prefixes.add((metadata["schema"].casefold(), metadata["alias"].casefold()))

    def next_nonspace(index):
        while index < len(tokens) and tokens[index][0] == "space":
            index += 1
        return index

    output, params = [], []
    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        if kind not in {"word", "backtick"}:
            output.append(value)
            index += 1
            continue
        indexes = [index]
        names = [_ident(value) if kind == "backtick" else value]
        cursor = index
        while True:
            dot = next_nonspace(cursor + 1)
            if dot >= len(tokens) or tokens[dot][1] != ".":
                break
            name_index = next_nonspace(dot + 1)
            if name_index >= len(tokens) or tokens[name_index][0] not in {"word", "backtick"}:
                break
            part_kind, part = tokens[name_index]
            indexes.extend((dot, name_index))
            names.append(_ident(part) if part_kind == "backtick" else part)
            cursor = name_index
        after = next_nonspace(indexes[-1] + 1)
        if len(names) == 1 and after < len(tokens) and tokens[after][1] == "(":
            output.extend(tokens[i][1] for i in range(index, indexes[-1] + 1))
            index = indexes[-1] + 1
            continue
        column = names[-1]
        key = columns.get(column.casefold())
        if len(names) > 1 and tuple(part.casefold() for part in names[:-1]) not in allowed_prefixes:
            return None, [], f"unsupported condition: column qualifier '{'.'.join(names[:-1])}' refers to another table"
        if key is not None:
            if for_table:
                output.append(_quote(key))
            else:
                output.append("%s")
                params.append(row[key])
            index = indexes[-1] + 1
            continue
        if len(names) > 1:
            return None, [], f"unsupported condition: column '{column}' is missing from the sampled table"
        if column.casefold() == "interval":
            return None, [], "unsupported condition: date math using INTERVAL"
        if column.casefold() in _KEYWORDS:
            output.append(value)
            index += 1
            continue
        previous = next((tokens[j][1].casefold() for j in range(index - 1, -1, -1) if tokens[j][0] != "space"), "")
        if previous == "collate" or column.casefold().startswith("_utf"):
            output.append(value)
            index += 1
            continue
        return None, [], f"unsupported condition: unknown column or identifier '{column}'"
    return "".join(output), params, None


def _unwrap(expression: str) -> str:
    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        depth, quote, wrapped = 0, None, True
        for index, char in enumerate(expression):
            if quote:
                if char == quote and (index == 0 or expression[index - 1] != "\\"):
                    quote = None
            elif char in {"'", '"', "`"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wrapped = False
                    break
        if not wrapped:
            break
        expression = expression[1:-1].strip()
    return expression


def _balanced_sample(cursor, metadata: dict, base_rows: list[dict], condition: str):
    if not _safe_expression(condition):
        return base_rows[:10]
    known_columns = base_rows[0] if base_rows else {column: None for column in metadata["columns"]}
    expression, _, error = _rewrite(condition, known_columns, metadata, for_table=True)
    if error or not expression:
        return base_rows[:10]
    table_sql = f"{_quote(metadata['schema'])}.{_quote(metadata['table'])}"
    try:
        cursor.execute(f"SELECT * FROM {table_sql} WHERE ({expression}) LIMIT 5")
        passed = _rows_from_cursor(cursor, cursor.fetchall())
        cursor.execute(f"SELECT * FROM {table_sql} WHERE NOT COALESCE(({expression}), 0) LIMIT 5")
        failed = _rows_from_cursor(cursor, cursor.fetchall())
    except mysql.connector.Error:
        return base_rows[:10]
    selected, seen = [], set()
    for row in passed + failed + base_rows:
        fingerprint = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if fingerprint not in seen:
            selected.append(row)
            seen.add(fingerprint)
        if len(selected) >= 10:
            break
    return selected


def _metadata(cursor, ref, cache, samples, default_schema, conditions):
    requested_schema, requested_table, alias = ref
    schema = requested_schema or default_schema
    cache_key = (schema.casefold(), requested_table.casefold(), alias.casefold())
    if cache_key in cache:
        return cache[cache_key]
    cursor.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s LIMIT 1",
        (schema, requested_table),
    )
    found = cursor.fetchone()
    if not found:
        cache[cache_key] = None
        return None
    table = str(found[0])
    cursor.execute(f"SELECT COUNT(*) FROM {_quote(schema)}.{_quote(table)}")
    count = cursor.fetchone()
    rows = _source_rows(samples, schema, table, alias, default_schema)
    if rows is None:
        cursor.execute(f"SELECT * FROM {_quote(schema)}.{_quote(table)} LIMIT 10")
        rows = _rows_from_cursor(cursor, cursor.fetchmany(10))
    else:
        rows = [_bare_row(row) for row in rows[:10]]
    columns = list(rows[0]) if rows else []
    if not columns:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        columns = [str(row[0]) for row in cursor.fetchall()]
    metadata = {
        "schema": schema,
        "table": table,
        "alias": alias or table,
        "source_id": f"{schema.casefold()}.{table.casefold()}|{alias.casefold()}",
        "columns": columns,
        "sample": [],
        "table_row_count": int(count[0]) if count else 0,
    }
    if conditions:
        rows = _balanced_sample(cursor, metadata, rows, conditions[0])
    for index, row in enumerate(rows[:10]):
        metadata["sample"].append({"rid": f"{alias}#{index + 1}", "values": _bare_row(row)})
    cache[cache_key] = metadata
    return metadata


def _sort_rids(metadata, records, rids, order_by=None, limit=None):
    selected = [rid for rid in rids if rid in records]
    if order_by:
        columns = {str(name).casefold(): str(name) for name in metadata["columns"]}
        sort_keys = []
        for item in order_by.split(","):
            match = re.fullmatch(r"(?:[\w$`]+\s*\.\s*)*(`(?:``|[^`])+`|[\w$]+)(?:\s+(ASC|DESC))?", item.strip(), re.I)
            if not match or _ident(match.group(1)).casefold() not in columns:
                return None
            sort_keys.append((columns[_ident(match.group(1)).casefold()], (match.group(2) or "ASC").upper() == "DESC"))

        def value_key(value):
            if value is None:
                return (0, 0)
            if isinstance(value, bool):
                return (1, int(value))
            if isinstance(value, (int, float, Decimal)):
                return (1, float(value))
            return (2, str(value).casefold())

        for column, descending in reversed(sort_keys):
            selected.sort(key=lambda rid: value_key(records[rid]["values"].get(column)), reverse=descending)
    return selected[:limit] if limit is not None else selected


def _join_reference(value: str, aliases: dict):
    parts = [_ident(part.strip()) for part in re.split(r"\s*\.\s*", value)]
    if len(parts) < 2:
        return None
    qualifier, column = parts[-2], parts[-1]
    ref = aliases.get(qualifier.casefold())
    if ref:
        return ref[2].casefold(), column.casefold()
    if len(parts) >= 3:
        table = parts[-2]
        return table.casefold(), column.casefold()
    return qualifier.casefold(), column.casefold()


def _join_clauses(query: str, aliases: dict):
    clauses = []
    on_pattern = re.compile(
        r"\bON\b([\s\S]*?)(?=\b(?:(?:LEFT|RIGHT|INNER|FULL|CROSS|NATURAL)\s+)?JOIN\b"
        r"|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|;|$)",
        re.I,
    )
    for clause in on_pattern.finditer(query or ""):
        predicates = []
        for match in _JOIN_EQUALITY.finditer(clause.group(1)):
            left = _join_reference(match.group("left"), aliases)
            right = _join_reference(match.group("right"), aliases)
            if left and right and left[0] != right[0]:
                predicates.append((left, right))
        if predicates:
            clauses.append(predicates)
    return clauses


def _join_value_equal(left, right) -> bool:
    if left is None or right is None:
        return False
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return str(left).strip().casefold() == str(right).strip().casefold()


def _join_sample_pairs(query: str, aliases: dict, records: dict):
    groups = {}
    for rid, record in records.items():
        alias = str(record.get("alias") or record.get("table") or "").casefold()
        if alias:
            groups.setdefault(alias, []).append((rid, record))
    for predicates in _join_clauses(query, aliases):
        alias_names = {alias for predicate in predicates for alias, _ in predicate}
        if len(alias_names) != 2 or not alias_names.issubset(groups):
            continue
        left_alias, right_alias = predicates[0][0][0], predicates[0][1][0]
        relevant = [p for p in predicates if {p[0][0], p[1][0]} == alias_names]
        pairs = []
        for left_rid, left_record in groups[left_alias]:
            for right_rid, right_record in groups[right_alias]:
                matched = True
                for (alias_a, column_a), (alias_b, column_b) in relevant:
                    rec_a = left_record if alias_a == left_alias else right_record
                    rec_b = left_record if alias_b == left_alias else right_record
                    key_a = next((key for key in rec_a["values"] if key.casefold() == column_a), None)
                    key_b = next((key for key in rec_b["values"] if key.casefold() == column_b), None)
                    if key_a is None or key_b is None or not _join_value_equal(rec_a["values"][key_a], rec_b["values"][key_b]):
                        matched = False
                        break
                if matched:
                    pairs.append((left_rid, right_rid))
        return pairs, True, f"Matched {len(pairs)} sampled row pairs using the JOIN equality condition."
    return [], False, "unsupported condition: JOIN equality could not be mapped to sampled table aliases"


def build_row_flow(plan_tree: dict, conn, source_samples: dict | None = None) -> list[dict]:
    query = str(plan_tree.get("_flow_query") or "")
    samples = source_samples or {}
    aliases = _aliases(query)
    nodes = _postorder(plan_tree)
    metadata_cache, records, steps = {}, {}, []
    current_rids = []
    with closing(conn.cursor()) as cursor:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
        cursor.execute("SELECT DATABASE()")
        default_row = cursor.fetchone()
        default_schema = str(default_row[0]) if default_row else "sql_analyzer_test"

        conditions_by_source = {}
        for node in nodes:
            if _action(str(node.get("operator") or "")) == "filter":
                ref = _table_for_node(node, aliases)
                if ref:
                    conditions_by_source.setdefault(_identity(ref, default_schema), []).append(str(node.get("details") or ""))

        for node in nodes:
            operator = str(node.get("operator") or "Other")
            action = _action(operator)
            input_rids = list(current_rids)
            actual_rows = node.get("rows_total")
            if actual_rows is None:
                actual_rows = node.get("actual_rows")
            loops = node.get("loops") or 1
            est_rows = float(node.get("estimated_rows") or 0) * float(loops)
            ref = _table_for_node(node, aliases)
            conditions = conditions_by_source.get(_identity(ref, default_schema), []) if ref else []
            metadata = _metadata(cursor, ref, metadata_cache, samples, default_schema, conditions) if ref else None
            output_rids, row_results = [], {}
            evaluable, note = False, ""
            table_count = metadata["table_row_count"] if metadata else 0
            never_executed = bool(node.get("never_executed"))
            if action == "join" and not input_rids and float(actual_rows or 0) == 0:
                never_executed = True

            if never_executed:
                note = "Never executed: upstream returned 0 rows."
                row_results = {rid: False for rid in input_rids}
            elif action == "scan" and metadata:
                for item in metadata["sample"]:
                    rid = item["rid"]
                    records[rid] = {
                        "table": metadata["table"], "schema": metadata["schema"], "alias": metadata["alias"],
                        "source_id": metadata["source_id"], "values": item["values"],
                        "table_row_count": metadata["table_row_count"],
                    }
                output_rids = [item["rid"] for item in metadata["sample"]]
                row_results = {rid: True for rid in output_rids}
                evaluable = True
                note = f"Sampled {len(output_rids)} rows from {metadata['schema']}.{metadata['table']} as {metadata['alias']}."
            elif action == "scan":
                note = "unsupported condition: plan scan could not be resolved to a base table"
            elif action == "filter":
                condition = _unwrap(str(node.get("details") or ""))
                if not metadata:
                    note = "unsupported condition: the plan table could not be resolved to a base table"
                elif not _safe_expression(condition):
                    note = "unsupported condition: expression contains SQL that cannot be sampled safely"
                elif not input_rids:
                    note = "unsupported condition: no sample rows are available to evaluate"
                elif any(rid not in records or records[rid]["source_id"] != metadata["source_id"] for rid in input_rids):
                    note = "unsupported condition: sample rows belong to a different table or alias"
                else:
                    evaluation_error = None
                    for rid in input_rids:
                        expression, params, error = _rewrite(condition, records[rid]["values"], metadata)
                        if error:
                            evaluation_error = error
                            break
                        try:
                            cursor.execute(f"SELECT ({expression})", tuple(params))
                            result = cursor.fetchone()
                        except mysql.connector.Error:
                            evaluation_error = "unsupported condition: SQL expression could not be evaluated against the sampled row"
                            break
                        row_results[rid] = bool(result[0]) if result else False
                    if evaluation_error:
                        note = evaluation_error
                        row_results = {}
                    else:
                        output_rids = [rid for rid in input_rids if row_results.get(rid, False)]
                        evaluable = True
                        note = f"Evaluated the condition against {len(input_rids)} sampled rows with column qualifiers resolved."
            elif action == "join":
                pairs, evaluable, note = _join_sample_pairs(query, aliases, records)
                input_rids = [rid for pair in pairs for rid in pair]
                output_rids = list(input_rids)
                row_results = {rid: True for rid in output_rids}
            elif action == "sort" and metadata and input_rids:
                details = str(node.get("details") or "").strip()
                order_by = re.sub(r",\s*limit input to\b.*$", "", details, flags=re.I).strip()
                limit_match = re.search(r"limit input to\s+(\d+)\s+row", details, re.I)
                if all(rid in records and records[rid]["source_id"] == metadata["source_id"] for rid in input_rids):
                    sorted_rids = _sort_rids(metadata, records, input_rids, order_by, int(limit_match.group(1)) if limit_match else None)
                    if sorted_rids is not None:
                        output_rids = sorted_rids
                        row_results = {rid: True for rid in input_rids}
                        evaluable = True
                        note = "Applied ORDER BY to the sampled rows."
                if not evaluable:
                    note = "unsupported condition: sort expression could not be resolved against sampled columns"
            elif action == "limit" and metadata and input_rids:
                limit_match = re.search(r"(?:limit\s*:\s*|^\s*)(\d+)\s+row", str(node.get("details") or ""), re.I)
                if limit_match and all(rid in records and records[rid]["source_id"] == metadata["source_id"] for rid in input_rids):
                    output_rids = input_rids[:int(limit_match.group(1))]
                    output = set(output_rids)
                    row_results = {rid: rid in output for rid in input_rids}
                    evaluable = True
                    note = f"Applied LIMIT {limit_match.group(1)} to the sampled rows."
                if not evaluable:
                    note = "unsupported condition: LIMIT could not be applied to this sample"
            else:
                note = f"{operator} is not evaluated on this sample; survivors are scaled from plan counts."

            if not evaluable and not never_executed:
                children = node.get("children", [])
                child_actual = sum(float(child.get("rows_total") or child.get("actual_rows") or 0) for child in children)
                survivor_count = min(len(input_rids), max(0, round(len(input_rids) * float(actual_rows or 0) / child_actual))) if child_actual > 0 else 0
                output_rids = input_rids[:survivor_count]
                selected = set(output_rids)
                row_results = {rid: rid in selected for rid in input_rids}
                note = f"{note or 'unsupported condition: operator could not be mapped to sampled rows'} Scaled survivors from actual plan row counts ({survivor_count} of {len(input_rids)} samples)."

            row_data = {rid: records[rid] for rid in dict.fromkeys(input_rids + output_rids) if rid in records}
            steps.append({
                "node_id": str(node.get("id") or ""), "op": action,
                "label": f"{operator}: {node.get('details') or ''}".rstrip(": "),
                "condition": str(node.get("details") or "") if action == "filter" else "",
                "input_rids": input_rids, "output_rids": output_rids, "evaluable": evaluable,
                "est_rows": est_rows, "actual_rows": float(actual_rows or 0), "table_row_count": table_count,
                "never_executed": never_executed, "row_results": row_results, "note": note, "row_data": row_data,
            })
            current_rids = list(output_rids)
    return steps
