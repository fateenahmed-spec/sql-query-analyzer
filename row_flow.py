"""Build branch-aware row samples for physical MySQL plan operators."""

from __future__ import annotations

import re
from contextlib import closing
from decimal import Decimal
import time

import mysql.connector
import sqlparse
from security import query_source_references


_SCAN_OPERATORS = {
    "Table scan", "Index scan", "Index range scan", "Index lookup",
    "Single-row index lookup", "Covering index lookup", "Covering index scan",
    "Single-row covering index lookup",
}
_FORBIDDEN = re.compile(r";|--|#|/\*|\*/|\b(?:SELECT|FROM|UNION|WITH|EXISTS|INTO|OUTFILE|LOAD_FILE)\b", re.I)
_JOIN_REF = r"(?:`(?:``|[^`])+`|[\w$]+)"
_JOIN_EQUALITY = re.compile(
    rf"(?P<left>{_JOIN_REF}(?:\s*\.\s*{_JOIN_REF})+)\s*=\s*"
    rf"(?P<right>{_JOIN_REF}(?:\s*\.\s*{_JOIN_REF})+)",
    re.I,
)
_SUBQUERY_IN = re.compile(
    rf"(?P<outer>{_JOIN_REF}(?:\s*\.\s*{_JOIN_REF})+)\s+(?:NOT\s+)?IN\s*\(\s*SELECT\s+"
    rf"(?P<inner>{_JOIN_REF}(?:\s*\.\s*{_JOIN_REF})+)",
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
_SAFE_SCALAR_FUNCTIONS = {
    "count", "sum", "avg", "min", "max",
    "abs", "acos", "asin", "atan", "atan2", "ceil", "ceiling", "floor", "round", "truncate",
    "exp", "ln", "log", "log10", "log2", "pow", "power", "sqrt", "sign", "greatest", "least",
    "coalesce", "ifnull", "nullif", "if", "isnull", "lower", "upper", "lcase", "ucase", "length",
    "char_length", "character_length", "octet_length", "concat", "concat_ws", "left", "right",
    "substring", "substr", "mid", "replace", "trim", "ltrim", "rtrim", "reverse", "repeat",
    "locate", "instr", "strcmp", "ascii", "ord", "hex", "unhex", "bin", "date", "time", "year",
    "quarter", "month", "day", "dayofmonth", "dayofweek", "weekday", "dayofyear", "hour", "minute",
    "second", "microsecond", "datediff", "timestampdiff", "cast", "convert", "decimal", "char", "signed",
    "unsigned", "json_extract", "json_unquote", "json_contains", "json_type", "json_length",
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
    normalized = str(operator or "").casefold()
    if operator in _SCAN_OPERATORS or normalized == "rows fetched before execution":
        return "scan"
    if normalized == "zero rows":
        return "empty"
    if normalized == "filter":
        return "filter"
    if "join" in normalized:
        return "join"
    if normalized.startswith("aggregate") or normalized.startswith("group aggregate") or normalized.startswith("groupaggregate"):
        return "aggregate"
    if normalized.startswith("window aggregate") or normalized == "window":
        return "window"
    if normalized == "sort":
        return "sort"
    if normalized == "limit":
        return "limit"
    if normalized.startswith("materialize") and "deduplic" not in normalized and "deduplication" not in normalized:
        return "materialize"
    if normalized == "hash":
        return "hash"
    return "other"


def _aliases(query: str) -> dict[str, tuple[str | None, str, str]]:
    try:
        references = query_source_references(query or "")
    except Exception:
        references = []
    alias_values = {}
    table_values = {}
    ambiguous_aliases = set()
    ambiguous_tables = set()
    for source in references:
        schema = source["schema"]
        table = str(source["table"] or "")
        alias = str(source["alias"] or table)
        if not table:
            continue
        value = (schema, table, alias)
        alias_key = alias.casefold()
        previous_alias = alias_values.get(alias_key)
        if previous_alias is not None and previous_alias[:2] != value[:2]:
            ambiguous_aliases.add(alias_key)
        else:
            alias_values[alias_key] = value
        table_key = table.casefold()
        previous_table = table_values.get(table_key)
        if previous_table is not None and previous_table[2].casefold() != alias_key:
            ambiguous_tables.add(table_key)
        else:
            table_values[table_key] = value
    result = {key: value for key, value in alias_values.items() if key not in ambiguous_aliases}
    for key, value in table_values.items():
        if key not in ambiguous_tables and key not in ambiguous_aliases:
            result.setdefault(key, value)
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
    if found is None:
        possible = {
            value for value in aliases.values()
            if value[1].casefold() == name.casefold()
            and (not plan_schema or not value[0] or value[0].casefold() == plan_schema.casefold())
        }
        if len(possible) > 1:
            return None
        if possible:
            found = next(iter(possible))
    if found:
        if plan_schema and found[0] and found[0].casefold() != plan_schema.casefold():
            return (plan_schema, found[1], found[2])
        return (plan_schema or found[0], found[1], found[2])
    return (plan_schema, name, name)


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
    if not expression.strip() or _FORBIDDEN.search(expression):
        return False
    tokens = [(match.lastgroup or "other", match.group(0)) for match in _TOKEN.finditer(expression)]
    for index, (kind, value) in enumerate(tokens):
        if kind not in {"word", "backtick"}:
            continue
        if _ident(value).casefold() in _KEYWORDS:
            continue
        next_index = index + 1
        while next_index < len(tokens) and tokens[next_index][0] == "space":
            next_index += 1
        if next_index < len(tokens) and tokens[next_index][1] == "(":
            if _ident(value).casefold() not in _SAFE_SCALAR_FUNCTIONS:
                return False
    return True


def _column_value(values: dict, name: str):
    found = next((key for key in values if str(key).casefold() == name.casefold()), None)
    return (values[found], found) if found is not None else (None, None)


def _canonical_sql_expression(expression: str) -> str:
    text = re.sub(r"`", "", str(expression or "")).casefold()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"(?:[\w$]+\.)+([\w$]+)", r"\1", text)


def _rewrite_record(expression: str, record: dict):
    """Bind qualified or unambiguous columns from a base or joined sample row."""
    if not _safe_expression(expression):
        return None, [], "unsupported condition: expression contains SQL that cannot be sampled safely"
    tokens = [(match.lastgroup or "other", match.group(0)) for match in _TOKEN.finditer(expression)]
    qualified = record.get("qualified_values") or {}
    qualifier_aliases = record.get("qualifier_aliases") or {}
    fallback_values = record.get("values") or {}
    expression_values = record.get("expression_values") or {}
    params, output = [], []

    def next_nonspace(index):
        while index < len(tokens) and tokens[index][0] == "space":
            index += 1
        return index

    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        if kind not in {"word", "backtick"}:
            output.append(value)
            index += 1
            continue
        indexes = [index]
        names = [_ident(value)]
        cursor = index
        while True:
            dot = next_nonspace(cursor + 1)
            if dot >= len(tokens) or tokens[dot][1] != ".":
                break
            name_index = next_nonspace(dot + 1)
            if name_index >= len(tokens) or tokens[name_index][0] not in {"word", "backtick"}:
                break
            indexes.extend((dot, name_index))
            names.append(_ident(tokens[name_index][1]))
            cursor = name_index
        after = next_nonspace(indexes[-1] + 1)
        if len(names) == 1 and after < len(tokens) and tokens[after][1] == "(":
            depth = 0
            close = None
            for token_index in range(after, len(tokens)):
                if tokens[token_index][1] == "(":
                    depth += 1
                elif tokens[token_index][1] == ")":
                    depth -= 1
                    if depth == 0:
                        close = token_index
                        break
            if close is not None:
                function_call = "".join(item[1] for item in tokens[index:close + 1])
                canonical = _canonical_sql_expression(function_call)
                if canonical in expression_values:
                    output.append("%s")
                    params.append(expression_values[canonical])
                    index = close + 1
                    continue
            output.append(value)
            index += 1
            continue

        column = names[-1]
        source_values = None
        if len(names) > 1:
            qualifier = names[-2].casefold()
            alias = qualifier_aliases.get(qualifier, qualifier)
            source_values = next((values for key, values in qualified.items() if key.casefold() == alias.casefold()), None)
            if source_values is None:
                return None, [], f"unsupported condition: qualifier '{names[-2]}' is not present in this row"
        else:
            matches = []
            for values in qualified.values():
                _, key = _column_value(values, column)
                if key is not None:
                    matches.append(values)
            if len(matches) > 1:
                return None, [], f"unsupported condition: unqualified column '{column}' is ambiguous in this row"
            source_values = matches[0] if matches else fallback_values

        value_found, key = _column_value(source_values, column)
        if key is None:
            if column.casefold() in _KEYWORDS:
                output.append(value)
                index += 1
                continue
            if column.casefold() == "interval":
                return None, [], "unsupported condition: date math using INTERVAL"
            return None, [], f"unsupported condition: column '{column}' is missing from this sample row"
        output.append("%s")
        params.append(value_found)
        index = indexes[-1] + 1
    return "".join(output), params, None


def _sample_condition(cursor, condition: str, record: dict):
    expression, params, error = _rewrite_record(condition, record)
    if error:
        return None, error
    try:
        cursor.execute(f"SELECT ({expression})", tuple(params))
        value = cursor.fetchone()
        return (bool(value[0]) if value and value[0] is not None else False), None
    except mysql.connector.Error:
        return None, "unsupported condition: MySQL could not evaluate this expression for the sampled row"


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


def _metadata(ref, cache, samples, default_schema, cursor=None):
    requested_schema, requested_table, alias = ref
    schema = requested_schema or default_schema
    cache_key = (schema.casefold(), requested_table.casefold(), alias.casefold())
    if cache_key in cache:
        return cache[cache_key]
    table = requested_table
    rows = _source_rows(samples, schema, table, alias, default_schema)
    if rows is None and cursor is not None:
        cursor.execute(f"SELECT * FROM {_quote(schema)}.{_quote(table)} LIMIT 10")
        rows = _rows_from_cursor(cursor, cursor.fetchmany(10))
    rows = rows or []
    rows = [_bare_row(row) for row in rows[:10]]
    columns = list(rows[0]) if rows else []
    if not columns and cursor is not None:
        cursor.execute(f"SELECT * FROM {_quote(schema)}.{_quote(table)} LIMIT 0")
        columns = [_bare_column_name(column[0]) for column in (cursor.description or [])]
    metadata = {
        "schema": schema,
        "table": table,
        "alias": alias or table,
        "source_id": f"{schema.casefold()}.{table.casefold()}|{alias.casefold()}",
        "columns": columns,
        "sample": [],
        "table_row_count": 0,
    }
    for index, row in enumerate(rows[:10]):
        metadata["sample"].append({"rid": f"{alias}#{index + 1}", "values": _bare_row(row)})
    cache[cache_key] = metadata
    return metadata


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
    query = sqlparse.format(query or "", strip_comments=True, reindent=False)
    lexed = []
    depth = 0
    for match in _TOKEN.finditer(query):
        kind, value = match.lastgroup or "other", match.group(0)
        if kind == "space" or kind == "string":
            continue
        if value == "(":
            lexed.append(("(", match.start(), match.end(), depth))
            depth += 1
        elif value == ")":
            lexed.append((")", match.start(), match.end(), depth))
            depth = max(0, depth - 1)
        elif kind in {"word", "backtick"}:
            lexed.append((_ident(value).upper(), match.start(), match.end(), depth))

    clauses = []
    join_indexes = [index for index, token in enumerate(lexed) if token[0] == "JOIN"]
    boundaries = {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "WINDOW", "FOR"}
    for join_position, join_index in enumerate(join_indexes):
        join_token = lexed[join_index]
        join_depth = join_token[3]
        next_join_start = lexed[join_indexes[join_position + 1]][1] if join_position + 1 < len(join_indexes) else len(query)
        join_type = "inner"
        if join_index and lexed[join_index - 1][3] == join_depth and lexed[join_index - 1][0] in {"LEFT", "RIGHT", "CROSS", "NATURAL"}:
            join_type = lexed[join_index - 1][0].casefold()
        on_token = next((token for token in lexed[join_index + 1:] if token[3] == join_depth and token[1] < next_join_start and token[0] == "ON"), None)
        if on_token is None:
            clauses.append({"type": join_type, "condition": "", "predicates": [], "depth": join_depth})
            continue
        end = next_join_start
        for token in lexed:
            if token[1] <= on_token[2] or token[1] >= end or token[3] != join_depth:
                continue
            if token[0] in boundaries:
                end = token[1]
                break
        condition = query[on_token[2]:end].strip()
        predicates = []
        for equality in _JOIN_EQUALITY.finditer(condition):
            left = _join_reference(equality.group("left"), aliases)
            right = _join_reference(equality.group("right"), aliases)
            if left and right and left[0] != right[0]:
                predicates.append((left, right))
        clauses.append({"type": join_type, "condition": condition, "predicates": predicates, "depth": join_depth})
    return clauses


def _join_value_equal(left, right) -> bool:
    if left is None or right is None:
        return False
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return str(left).strip().casefold() == str(right).strip().casefold()


def _record_source_aliases(record: dict) -> set[str]:
    return {str(alias).casefold() for alias in record.get("source_aliases", [])}


def _merge_records(left: dict, right: dict, rid: str) -> dict:
    qualified = dict(left.get("qualified_values") or {})
    qualified.update(right.get("qualified_values") or {})
    qualifier_aliases = dict(left.get("qualifier_aliases") or {})
    qualifier_aliases.update(right.get("qualifier_aliases") or {})
    aliases = list(dict.fromkeys(list(left.get("source_aliases", [])) + list(right.get("source_aliases", []))))
    values = {}
    for alias, columns in qualified.items():
        for column, value in columns.items():
            values[f"{alias}.{column}"] = value
    return {
        "table": "Joined rows",
        "schema": None,
        "alias": "+".join(aliases),
        "source_aliases": aliases,
        "qualified_values": qualified,
        "qualifier_aliases": qualifier_aliases,
        "values": values,
        "source_id": "join:" + ",".join(aliases),
        "expression_values": {**(left.get("expression_values") or {}), **(right.get("expression_values") or {})},
        "rid": rid,
    }


def _single_source_record(metadata: dict, item: dict) -> dict:
    alias = metadata["alias"]
    values = dict(item["values"])
    return {
        "table": metadata["table"],
        "schema": metadata["schema"],
        "alias": alias,
        "source_aliases": [alias],
        "qualified_values": {alias: values},
        "qualifier_aliases": {
            alias.casefold(): alias,
            metadata["table"].casefold(): alias,
            metadata["schema"].casefold(): alias,
        },
        "source_id": metadata["source_id"],
        "expression_values": {},
        "values": values,
        "table_row_count": metadata["table_row_count"],
        "sample_origin": "sampled base table row",
    }


def _schema_aliases(schema: list[dict]) -> set[str]:
    return {str(item.get("alias") or "").casefold() for item in schema if item.get("alias")}


def _join_clause_for_branches(query: str, aliases: dict, left_rows: list[str], right_rows: list[str], records: dict, operator: str, left_schema=None, right_schema=None):
    left_aliases = set().union(*(_record_source_aliases(records[rid]) for rid in left_rows if rid in records)) if left_rows else set()
    right_aliases = set().union(*(_record_source_aliases(records[rid]) for rid in right_rows if rid in records)) if right_rows else set()
    left_aliases.update(_schema_aliases(left_schema or []))
    right_aliases.update(_schema_aliases(right_schema or []))
    for clause in _join_clauses(query, aliases):
        if not clause["condition"]:
            continue
        referenced = set()
        for first, second in re.findall(r"(`(?:``|[^`])+`|[\w$]+)\s*\.\s*(`(?:``|[^`])+`|[\w$]+)", clause["condition"]):
            key = _ident(first).casefold()
            source = aliases.get(key)
            referenced.add((source[2] if source else key).casefold())
        if referenced & left_aliases and referenced & right_aliases:
            return clause
    if "semijoin" in operator.casefold() or "antijoin" in operator.casefold():
        predicates = []
        for match in _JOIN_EQUALITY.finditer(query or ""):
            left = _join_reference(match.group("left"), aliases)
            right = _join_reference(match.group("right"), aliases)
            if not left or not right:
                continue
            if (left[0].casefold() in left_aliases and right[0].casefold() in right_aliases) or (
                right[0].casefold() in left_aliases and left[0].casefold() in right_aliases
            ):
                predicates.append(match.group(0))
        for match in _SUBQUERY_IN.finditer(query or ""):
            outer = _join_reference(match.group("outer"), aliases)
            inner = _join_reference(match.group("inner"), aliases)
            if not outer or not inner:
                continue
            if (outer[0] in left_aliases and inner[0] in right_aliases) or (
                inner[0] in left_aliases and outer[0] in right_aliases
            ):
                predicates.append(f"{match.group('outer')} = {match.group('inner')}")
        if predicates:
            return {"type": "inner", "condition": " AND ".join(predicates), "predicates": []}
    if "cross" in operator.casefold():
        return {"type": "cross", "condition": "", "predicates": []}
    return None


def _join_branch_outputs(cursor, query, aliases, operator, node_id, left_ids, right_ids, records, evaluate_condition, left_schema=None, right_schema=None):
    left_schema = left_schema or []
    right_schema = right_schema or []
    clause = _join_clause_for_branches(query, aliases, left_ids, right_ids, records, operator, left_schema, right_schema)
    is_cross = "cross" in operator.casefold()
    if clause is None and not is_cross:
        return [], [], False, "The JOIN condition could not be associated with these two plan branches."
    join_type = str(clause.get("type") if clause else "cross").casefold()
    if "left" in operator.casefold():
        join_type = "left"
    if "right" in operator.casefold():
        join_type = "right"
    semi = "semijoin" in operator.casefold()
    anti = "antijoin" in operator.casefold()
    if clause and clause.get("condition") and not _safe_expression(clause["condition"]):
        return [], [], False, "The JOIN condition contains a subquery or unsafe expression that cannot be evaluated on this sample."

    left_items = [(rid, records[rid]) for rid in left_ids if rid in records]
    right_items = [(rid, records[rid]) for rid in right_ids if rid in records]
    matches, output, matched_left, matched_right, semi_output = [], [], set(), set(), set()
    for left_rid, left_record in left_items:
        for right_rid, right_record in right_items:
            merged = _merge_records(left_record, right_record, "")
            passed = True
            if clause and clause.get("condition"):
                passed, error = evaluate_condition(clause["condition"], merged)
                if error:
                    return [], [], False, error
            if not passed:
                continue
            matched_left.add(left_rid)
            matched_right.add(right_rid)
            matches.append([left_rid, right_rid])
            if anti:
                continue
            if semi:
                if left_rid not in semi_output:
                    output.append({"rid": f"{node_id}#sample{len(output) + 1}", "record": left_record})
                    semi_output.add(left_rid)
                continue
            rid = f"{node_id}#sample{len(output) + 1}"
            merged["rid"] = rid
            output.append({"rid": rid, "record": merged})
            if len(output) >= 10:
                break
        if len(output) >= 10:
            break

    if anti:
        for left_rid, left_record in left_items:
            if left_rid not in matched_left:
                output.append({"rid": left_rid, "record": left_record})
    elif join_type in {"left", "right"}:
        preserved = left_items if join_type == "left" else right_items
        nullable_items = right_items if join_type == "left" else left_items
        nullable_schema = right_schema if join_type == "left" else left_schema
        matched = matched_left if join_type == "left" else matched_right
        for preserved_rid, preserved_record in preserved:
            if preserved_rid in matched or len(output) >= 10:
                continue
            null_qualified = {}
            null_qualifiers = {}
            if nullable_items:
                template = nullable_items[0][1]
                null_qualified = {
                    alias: {column: None for column in values}
                    for alias, values in (template.get("qualified_values") or {}).items()
                }
                null_qualifiers = dict(template.get("qualifier_aliases") or {})
            elif nullable_schema:
                for item in nullable_schema:
                    alias = str(item.get("alias") or "")
                    null_qualified[alias] = {str(column): None for column in item.get("columns", [])}
                    null_qualifiers.update(item.get("qualifier_aliases") or {alias.casefold(): alias})
            null_side = {
                "qualified_values": null_qualified,
                "qualifier_aliases": null_qualifiers,
                "source_aliases": list(null_qualified),
                "values": {},
            }
            if join_type == "left":
                merged = _merge_records(preserved_record, null_side, "")
                other_rid = None
            else:
                merged = _merge_records(null_side, preserved_record, "")
                other_rid = None
            rid = f"{node_id}#sample{len(output) + 1}"
            merged["rid"] = rid
            merged["unmatched_side"] = "right" if join_type == "left" else "left"
            output.append({"rid": rid, "record": merged})
            matches.append([preserved_rid, other_rid] if join_type == "left" else [other_rid, preserved_rid])

    row_results = []
    for pair in matches:
        row_results.extend(item for item in pair if item)
    return output, matches, True, f"Combined {len(output)} matching sampled row pairs using the JOIN condition."


def _python_join_match(condition: str, record: dict):
    equalities = list(_JOIN_EQUALITY.finditer(condition or ""))
    if not equalities:
        return None
    remainder = _JOIN_EQUALITY.sub("", condition)
    remainder = re.sub(r"\bAND\b|[()]", "", remainder, flags=re.I).strip()
    if remainder:
        return None
    for equality in equalities:
        left = equality.group("left")
        right = equality.group("right")
        left_value, left_found = _record_column(record, left)
        right_value, right_found = _record_column(record, right)
        if not left_found or not right_found:
            return None
        if not _join_value_equal(left_value, right_value):
            return False
    return True


def _sql_words(sql: str) -> list[tuple[str, int, int, int]]:
    words = []
    depth = 0
    for match in _TOKEN.finditer(sql or ""):
        kind, value = match.lastgroup or "other", match.group(0)
        if kind in {"space", "string"}:
            continue
        if value == "(":
            words.append(("(", match.start(), match.end(), depth))
            depth += 1
        elif value == ")":
            words.append((")", match.start(), match.end(), depth))
            depth = max(0, depth - 1)
        elif kind in {"word", "backtick"}:
            words.append((_ident(value).upper(), match.start(), match.end(), depth))
        elif value == ",":
            words.append((",", match.start(), match.end(), depth))
    return words


def _outer_query_parts(query: str):
    words = _sql_words(query)
    main_select = next((word for word in words if word[0] == "SELECT" and word[3] == 0), None)
    if main_select is None:
        return None
    depth = main_select[3]
    clauses = {}
    for index, word in enumerate(words):
        if word[3] != depth or word[1] < main_select[2]:
            continue
        if word[0] in {"FROM", "WHERE", "HAVING", "LIMIT", "OFFSET", "UNION"}:
            clauses.setdefault(word[0], word[1])
        if word[0] == "GROUP" and index + 1 < len(words) and words[index + 1][0] == "BY" and words[index + 1][3] == depth:
            clauses.setdefault("GROUP BY", word[1])
        if word[0] == "ORDER" and index + 1 < len(words) and words[index + 1][0] == "BY" and words[index + 1][3] == depth:
            clauses.setdefault("ORDER BY", word[1])
    end = next((position for clause, position in clauses.items() if clause == "UNION"), len(query))
    from_position = clauses.get("FROM")
    if from_position is None:
        return None
    positions = sorted((position, clause) for clause, position in clauses.items() if position < end)
    boundaries = {clause: next((p for p, _ in positions if p > position), end) for position, clause in positions}
    def chunk(name):
        position = clauses.get(name)
        return query[position:boundaries[name]].strip() if position is not None and name in boundaries else ""
    select_end = main_select[2]
    select_list = query[select_end:from_position].strip()
    return {
        "select_list": select_list,
        "from": chunk("FROM"),
        "where": chunk("WHERE"),
        "group": chunk("GROUP BY"),
        "having": chunk("HAVING"),
        "order": chunk("ORDER BY"),
        "limit": chunk("LIMIT"),
    }


def _select_scope_texts(query: str) -> list[str]:
    words = _sql_words(query)
    scopes = []
    for index, word in enumerate(words):
        if word[0] != "SELECT":
            continue
        depth = word[3]
        end = len(query.rstrip().rstrip(";"))
        for tail in words[index + 1:]:
            if (tail[0] == ")" or tail[0] in {"UNION", ";"}) and tail[3] == depth:
                end = tail[1]
                break
        scope = query[word[1]:end].strip()
        if scope:
            scopes.append(scope)
    return scopes


def _split_sql_list(text: str) -> list[str]:
    parts, start, depth, quote = [], 0, 0, None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote != "`":
                index += 2
                continue
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts


def _expression_alias(expression: str) -> tuple[str, str]:
    match = re.match(r"(?is)^(.*?)(?:\s+AS\s+(`(?:``|[^`])+`|[\w$]+))$", expression.strip())
    if match:
        return match.group(1).strip(), _ident(match.group(2))
    match = re.match(r"(?is)^(.*\))\s+(`?(?:[A-Za-z_$][\w$]*)`?)$", expression.strip())
    if match:
        return match.group(1).strip(), _ident(match.group(2))
    bare = expression.strip()
    return bare, _ident(bare.split(".")[-1])


def _record_column(record: dict, expression: str):
    expression = expression.strip()
    match = re.fullmatch(r"(?:`?([\w$]+)`?\s*\.\s*)*`?([\w$]+)`?", expression)
    if not match:
        return None, False
    column = match.group(2)
    qualifier_matches = list(re.finditer(r"`?([\w$]+)`?\s*\.", expression))
    qualified = record.get("qualified_values") or {}
    if qualifier_matches:
        qualifier = qualifier_matches[-1].group(1).casefold()
        alias = (record.get("qualifier_aliases") or {}).get(qualifier, qualifier)
        values = next((value for key, value in qualified.items() if key.casefold() == str(alias).casefold()), None)
        if values is None:
            return None, False
        value, key = _column_value(values, column)
        return value, key is not None
    matches = []
    for values in qualified.values():
        value, key = _column_value(values, column)
        if key is not None:
            matches.append(value)
    if len(matches) == 1:
        return matches[0], True
    value, key = _column_value(record.get("values") or {}, column)
    return (value, True) if key is not None and not matches else (None, False)


def _aggregate_value(function: str, values: list):
    non_null = [value for value in values if value is not None]
    name = function.casefold()
    if name == "count":
        return len(values) if not values or values[0] is not _STAR else len(non_null)
    if name == "sum":
        return sum(non_null) if non_null else None
    if name == "avg":
        return (sum(non_null) / len(non_null)) if non_null else None
    if name == "min":
        return min(non_null) if non_null else None
    if name == "max":
        return max(non_null) if non_null else None
    raise ValueError("unsupported aggregate")


_STAR = object()


def _aggregate_sample(query: str, input_rids: list[str], records: dict, node_id: str):
    candidates = [_outer_query_parts(scope) for scope in _select_scope_texts(query)]
    parts = next((candidate for candidate in candidates if candidate and (candidate["group"] or re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", candidate["select_list"], re.I))), None)
    if not parts or not parts["group"] and not re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", parts["select_list"], re.I):
        return [], False, "The aggregate expression could not be mapped to the sampled SQL projection."
    group_text = parts["group"][len("GROUP BY"):].strip() if parts["group"] else ""
    group_exprs = _split_sql_list(group_text)
    items = []
    for expression in _split_sql_list(parts["select_list"]):
        expr, alias = _expression_alias(expression)
        aggregate_match = re.fullmatch(r"(?is)(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(\*|.+?)\s*\)", expr)
        if aggregate_match:
            function, column = aggregate_match.groups()
            items.append((alias, function.upper(), column.strip(), expr))
        else:
            simple = re.fullmatch(r"(?is)(?:`?[\w$]+`?\s*\.\s*)*`?[\w$]+`?", expr)
            if not simple:
                return [], False, f"Aggregate output expression '{alias}' is outside the sample evaluator's supported forms."
            items.append((alias, "COLUMN", expr, expr))
    groups = {}
    for rid in input_rids:
        record = records.get(rid)
        if record:
            key_values = []
            for expression in group_exprs:
                value, found = _record_column(record, expression)
                if not found:
                    return [], False, f"GROUP BY expression '{expression}' is missing or ambiguous in the sample rows."
                key_values.append(value)
            groups.setdefault(tuple(key_values), []).append(record)
    if not group_exprs and not groups:
        groups[()] = []
    output = []
    for group_number, (group_key, group_records) in enumerate(groups.items(), 1):
        values, expression_values = {}, {}
        for name, function, column, source_expression in items:
            if function == "COLUMN":
                value, found = _record_column(group_records[0], column) if group_records else (None, False)
                if not found:
                    return [], False, f"Grouped output column '{column}' is missing from the sample rows."
                values[name] = value
                continue
            if column == "*":
                aggregate_input = [_STAR] * len(group_records)
                values[name] = len(aggregate_input) if function == "COUNT" else None
            else:
                aggregate_input = []
                for record in group_records:
                    value, found = _record_column(record, column)
                    if not found:
                        return [], False, f"Aggregate input '{column}' is missing or ambiguous in the sample rows."
                    aggregate_input.append(value)
                try:
                    values[name] = _aggregate_value(function, aggregate_input)
                except (TypeError, ValueError):
                    return [], False, f"Aggregate function {function} could not be applied to these sample values."
            expression_values[_canonical_sql_expression(source_expression)] = values[name]
        output.append({"rid": f"{node_id}#sample{group_number}", "record": {
            "table": "Sample aggregate", "alias": "aggregate", "source_aliases": [],
            "qualified_values": {}, "qualifier_aliases": {}, "values": values,
            "expression_values": expression_values,
            "sample_origin": "aggregation calculated over sampled input rows",
        }})
    return output[:10], True, f"Calculated {len(output[:10])} groups from {len(input_rids)} sampled input rows; this is not the full-query aggregate."


def _window_sample(query: str, input_rids: list[str], records: dict, node_id: str):
    candidates = [_outer_query_parts(scope) for scope in _select_scope_texts(query)]
    parts = next((candidate for candidate in candidates if candidate and re.search(r"\bOVER\s*\(", candidate["select_list"], re.I)), None)
    if not parts:
        return [], False, "The window projection could not be isolated."
    items = _split_sql_list(parts["select_list"])
    window_items = []
    for expression in items:
        expr, alias = _expression_alias(expression)
        match = re.fullmatch(
            r"(?is)(ROW_NUMBER|RANK|DENSE_RANK|COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(.*?)\s*\)\s*OVER\s*\((.*?)\)", expr,
        )
        if not match:
            continue
        function, argument, over = match.groups()
        if re.search(r"\bROWS\b|\bRANGE\b|\bGROUPS\b", over, re.I):
            return [], False, "Explicit window frames are not simulated from a bounded sample."
        partition_match = re.search(r"\bPARTITION\s+BY\s+([\s\S]*?)(?=\bORDER\s+BY\b|$)", over, re.I)
        order_match = re.search(r"\bORDER\s+BY\s+([\s\S]*?)(?=$)", over, re.I)
        partition_exprs = _split_sql_list(partition_match.group(1)) if partition_match else []
        order_exprs = _split_sql_list(order_match.group(1)) if order_match else []
        if function.upper() in {"RANK", "DENSE_RANK", "ROW_NUMBER"} and not order_exprs:
            return [], False, f"{function.upper()} requires an ORDER BY for a meaningful sample calculation."
        window_items.append((function.upper(), argument.strip(), alias, partition_exprs, order_exprs))
    if not window_items:
        return [], False, "The window function is outside the supported sample evaluator forms."

    inputs = [records[rid] for rid in input_rids if rid in records]
    additions = [dict() for _ in inputs]
    for function, argument, alias, partition_exprs, order_exprs in window_items:
        partitions = {}
        for index, record in enumerate(inputs):
            key = []
            for expression in partition_exprs:
                value, found = _record_column(record, expression)
                if not found:
                    return [], False, f"Window partition column '{expression}' is missing or ambiguous in the sample rows."
                key.append(value)
            partitions.setdefault(tuple(key), []).append(index)
        for indexes in partitions.values():
            def sort_key(index):
                keys = []
                record = inputs[index]
                for expression in order_exprs:
                    value_expr = expression.strip()
                    descending = bool(re.search(r"\s+DESC\s*$", value_expr, re.I))
                    value_expr = re.sub(r"\s+(?:ASC|DESC)\s*$", "", value_expr, flags=re.I)
                    value, found = _record_column(record, value_expr)
                    if not found:
                        raise ValueError(value_expr)
                    keys.append((value, descending))
                return keys
            try:
                if order_exprs:
                    # Stable multi-key sorting while preserving MySQL null-first ASC behavior.
                    for expression in reversed(order_exprs):
                        descending = bool(re.search(r"\s+DESC\s*$", expression, re.I))
                        column = re.sub(r"\s+(?:ASC|DESC)\s*$", "", expression.strip(), flags=re.I)
                        indexes.sort(key=lambda idx: ( _record_column(inputs[idx], column)[0] is not None, _record_column(inputs[idx], column)[0]), reverse=descending)
            except (TypeError, ValueError):
                return [], False, "Window ORDER BY values could not be sorted consistently in the sample."
            if function in {"ROW_NUMBER", "RANK", "DENSE_RANK"}:
                previous = object()
                rank = 0
                dense = 0
                for position, row_index in enumerate(indexes, 1):
                    key = tuple(_record_column(inputs[row_index], re.sub(r"\s+(?:ASC|DESC)\s*$", "", item.strip(), flags=re.I))[0] for item in order_exprs)
                    if key != previous:
                        rank = position
                        dense += 1
                    additions[row_index][alias] = position if function == "ROW_NUMBER" else rank if function == "RANK" else dense
                    previous = key
            else:
                if order_exprs:
                    return [], False, "Ordered aggregate windows are not calculated without an explicit SQL window frame."
                aggregate_values = []
                if argument == "*":
                    aggregate_values = [_STAR] * len(indexes)
                else:
                    for row_index in indexes:
                        value, found = _record_column(inputs[row_index], argument)
                        if not found:
                            return [], False, f"Window input '{argument}' is missing or ambiguous in the sample rows."
                        aggregate_values.append(value)
                try:
                    aggregate = len(aggregate_values) if function == "COUNT" and argument == "*" else _aggregate_value(function, aggregate_values)
                except (TypeError, ValueError):
                    return [], False, f"Window function {function} could not be calculated over the sample."
                for row_index in indexes:
                    additions[row_index][alias] = aggregate

    output = []
    for index, record in enumerate(inputs):
        values = dict(record.get("values") or {})
        values.update(additions[index])
        rid = f"{node_id}#sample{index + 1}"
        clone = dict(record)
        clone.update(rid=rid, values=values, sample_origin="window calculation over sampled input rows")
        output.append({"rid": rid, "record": clone})
    return output[:10], True, f"Calculated window values on {len(output[:10])} sampled rows; the window may differ from the full query when its partition was truncated."


def build_row_flow(
    plan_tree: dict,
    conn,
    source_samples: dict | None = None,
    final_rows: list[dict] | None = None,
) -> list[dict]:
    query = str(plan_tree.get("_flow_query") or "")
    samples = source_samples or {}
    aliases = _aliases(query)
    metadata_cache, records, steps = {}, {}, []
    output_by_node = {}
    output_schema_by_node = {}
    output_exactness_by_node = {}
    scalar_evaluations = 0
    scalar_budget = 80
    scalar_deadline = time.monotonic() + 5.0
    with closing(conn.cursor()) as cursor:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
        cursor.execute("SELECT DATABASE()")
        default_row = cursor.fetchone()
        default_schema = str(default_row[0]) if default_row else "sql_analyzer_test"
        def evaluate_condition(condition, record):
            nonlocal scalar_evaluations
            python_result = _python_join_match(condition, record)
            if python_result is not None:
                return python_result, None
            if scalar_evaluations >= scalar_budget or time.monotonic() >= scalar_deadline:
                return None, "Sample evaluation budget or 5-second time budget reached; exact row outcome is unavailable."
            scalar_evaluations += 1
            return _sample_condition(cursor, condition, record)

        def visit(node: dict, parent_id: str | None = None):
            operator = str(node.get("operator") or "Other")
            action = _action(operator)
            node_id = str(node.get("id") or f"node_{len(steps) + 1}")
            child_nodes = list(node.get("children") or [])
            for child in child_nodes:
                visit(child, node_id)
            branches = [
                {
                    "node_id": str(child.get("id") or ""),
                    "operator": str(child.get("operator") or ""),
                    "output_rids": list(output_by_node.get(str(child.get("id") or ""), [])),
                    "schema": list(output_schema_by_node.get(str(child.get("id") or ""), [])),
                    "exactness": output_exactness_by_node.get(str(child.get("id") or ""), "not_evaluable"),
                }
                for child in child_nodes
            ]
            input_rids = list(dict.fromkeys(rid for branch in branches for rid in branch["output_rids"]))
            output_rids, row_results, matched_pairs = [], {}, []
            output_schema = []
            evaluable, exactness = False, "not_evaluable"
            note = ""
            never_executed = bool(node.get("never_executed"))
            table_count = 0

            if never_executed:
                note = "Never executed: MySQL reported that this physical operator had no input rows."
                exactness = "physical_plan_never_executed"
            elif action == "scan":
                ref = _table_ref(node, aliases)
                metadata = _metadata(ref, metadata_cache, samples, default_schema, cursor) if ref else None
                if metadata:
                    for item in metadata["sample"]:
                        rid = item["rid"]
                        records[rid] = _single_source_record(metadata, item)
                    output_rids = [item["rid"] for item in metadata["sample"]]
                    row_results = {rid: True for rid in output_rids}
                    evaluable = True
                    exactness = "sampled_source"
                    output_schema = [{
                        "alias": metadata["alias"],
                        "table": metadata["table"],
                        "columns": list(metadata["columns"]),
                        "qualifier_aliases": {
                            metadata["alias"].casefold(): metadata["alias"],
                            metadata["table"].casefold(): metadata["alias"],
                            metadata["schema"].casefold(): metadata["alias"],
                        },
                    }]
                    note = f"Source rows sampled from {metadata['schema']}.{metadata['table']} as {metadata['alias']}; this is a source sample, not a claim about rows consumed by this physical scan."
                else:
                    note = "This physical scan refers to a temporary or derived input that is not a base table sample. Its logical input is shown by its child nodes when available."
            elif action == "filter":
                output_schema = list(branches[0].get("schema", [])) if branches else []
                condition = _unwrap(str(node.get("details") or ""))
                if not input_rids:
                    note = "No sampled input rows reached this filter, so there are no records to evaluate."
                    evaluable = True
                    exactness = "sampled_input_empty"
                elif not _safe_expression(condition):
                    note = "The predicate contains a subquery or unsupported SQL expression; exact outcomes are unavailable for these sample rows."
                else:
                    evaluation_error = None
                    for rid in input_rids:
                        result, error = evaluate_condition(condition, records[rid])
                        if error:
                            evaluation_error = error
                            break
                        row_results[rid] = bool(result)
                    if evaluation_error:
                        row_results = {}
                        note = evaluation_error
                    else:
                        output_rids = [rid for rid in input_rids if row_results.get(rid)]
                        evaluable = True
                        exactness = "exact_for_sample"
                        note = f"MySQL evaluated the plan predicate against {len(input_rids)} sampled input rows."
            elif action == "join":
                if len(branches) != 2:
                    note = f"This physical JOIN has {len(branches)} child branches; pairwise output cannot be mapped safely."
                elif any(
                    not branch["output_rids"]
                    and branch["exactness"] == "not_evaluable"
                    for branch in branches
                ):
                    note = "At least one JOIN input branch has no evaluable sampled records, so no JOIN output was inferred."
                elif not branches[0]["output_rids"] or not branches[1]["output_rids"]:
                    left_outer_has_rows = "left" in operator.casefold() and bool(branches[0]["output_rids"])
                    right_outer_has_rows = "right" in operator.casefold() and bool(branches[1]["output_rids"])
                    if left_outer_has_rows or right_outer_has_rows:
                        joined, matched_pairs, evaluable, note = _join_branch_outputs(
                            cursor, query, aliases, operator, node_id,
                            branches[0]["output_rids"], branches[1]["output_rids"], records,
                            evaluate_condition, branches[0].get("schema"), branches[1].get("schema"),
                        )
                        if evaluable:
                            for item in joined:
                                output_rids.append(item["rid"])
                                records[item["rid"]] = item["record"]
                            output_schema = list(branches[0].get("schema", [])) + list(branches[1].get("schema", []))
                            exactness = "exact_for_sample"
                            matched_input_rids = {rid for pair in matched_pairs for rid in pair if rid}
                            row_results = {rid: rid in matched_input_rids for rid in input_rids}
                    else:
                        evaluable = True
                        exactness = "exact_for_sample"
                        note = "One sampled JOIN branch is empty, so this sampled join has no matching pairs."
                else:
                    joined, matched_pairs, evaluable, note = _join_branch_outputs(
                        cursor, query, aliases, operator, node_id,
                        branches[0]["output_rids"], branches[1]["output_rids"], records,
                        evaluate_condition, branches[0].get("schema"), branches[1].get("schema"),
                    )
                    if evaluable:
                        for item in joined:
                            output_rids.append(item["rid"])
                            records[item["rid"]] = item["record"]
                        exactness = "exact_for_sample"
                        output_schema = list(branches[0].get("schema", [])) + list(branches[1].get("schema", []))
                        matched_input_rids = {rid for pair in matched_pairs for rid in pair if rid}
                        row_results = {rid: rid in matched_input_rids for rid in input_rids}
            elif action == "sort":
                output_schema = list(branches[0].get("schema", [])) if branches else []
                details = str(node.get("details") or "").strip()
                order_by = re.sub(r",\s*limit input to\b.*$", "", details, flags=re.I).strip()
                limit_match = re.search(r"limit input to\s+(\d+)\s+row", details, re.I)
                sorted_rids = list(input_rids)
                sort_items = _split_sql_list(order_by)
                try:
                    for expression in reversed(sort_items):
                        descending = bool(re.search(r"\s+DESC\s*$", expression, re.I))
                        column = re.sub(r"\s+(?:ASC|DESC)\s*$", "", expression, flags=re.I).strip()
                        keyed = []
                        for rid in sorted_rids:
                            value, found = _record_column(records[rid], column)
                            if not found:
                                raise ValueError(column)
                            if value is None:
                                key = (0, 0, "")
                            elif isinstance(value, (int, float, Decimal)):
                                key = (1, 0, float(value))
                            else:
                                key = (1, 1, str(value).casefold())
                            keyed.append((key, rid))
                        keyed.sort(key=lambda item: item[0], reverse=descending)
                        sorted_rids = [rid for _, rid in keyed]
                    if limit_match:
                        sorted_rids = sorted_rids[:int(limit_match.group(1))]
                    output_rids = sorted_rids
                    row_results = {rid: True for rid in input_rids}
                    evaluable = True
                    exactness = "exact_for_sample"
                    note = f"Sorted {len(input_rids)} sampled rows using the plan ordering."
                except (TypeError, ValueError):
                    note = "The sort expression is not available or comparable in these sampled rows."
            elif action == "limit":
                output_schema = list(branches[0].get("schema", [])) if branches else []
                match = re.search(r"(?:limit\s*:\s*|^\s*)(\d+)\s+row", str(node.get("details") or ""), re.I)
                if match:
                    output_rids = input_rids[:int(match.group(1))]
                    kept = set(output_rids)
                    row_results = {rid: rid in kept for rid in input_rids}
                    evaluable = True
                    exactness = "exact_for_sample"
                    note = f"Applied the physical LIMIT to {len(input_rids)} sampled input rows."
                else:
                    note = "The physical plan does not expose a parseable LIMIT value for this node."
            elif action == "aggregate":
                aggregated, evaluable, note = _aggregate_sample(query, input_rids, records, node_id)
                if evaluable:
                    for item in aggregated:
                        output_rids.append(item["rid"])
                        records[item["rid"]] = item["record"]
                    exactness = "sampled_simulation"
                    if output_rids:
                        output_schema = [{"alias": "aggregate", "table": "Sample aggregate", "columns": list(records[output_rids[0]]["values"]), "qualifier_aliases": {}}]
            elif action == "window":
                windowed, evaluable, note = _window_sample(query, input_rids, records, node_id)
                if evaluable:
                    for item in windowed:
                        output_rids.append(item["rid"])
                        records[item["rid"]] = item["record"]
                    exactness = "sampled_simulation"
                    output_schema = list(branches[0].get("schema", [])) if branches else []
            elif action == "empty":
                output_rids = []
                evaluable = True
                exactness = "exact_for_sample"
                note = "MySQL's physical plan reports zero rows for this node."
            elif action in {"materialize", "hash"} and len(branches) == 1:
                output_rids = list(branches[0]["output_rids"])
                output_schema = list(branches[0].get("schema", []))
                row_results = {rid: True for rid in output_rids}
                evaluable = True
                exactness = "sample_passthrough"
                note = "This physical wrapper preserves its child's sampled rows; the wrapper itself does not change their values."
            elif len(branches) == 1 and action == "other" and not child_nodes[0].get("children"):
                note = f"{operator} is not mapped to a supported row transformation. No output rows were inferred."
            else:
                note = f"{operator} has no safe sample transformation mapping. No output rows were inferred."

            physical_rows = node.get("rows_total")
            if physical_rows is None:
                physical_rows = node.get("actual_rows")
            row_data = {rid: records[rid] for rid in dict.fromkeys(input_rids + output_rids) if rid in records}
            if action == "scan":
                logical_op = "Base table read" if output_rids else "Temporary or derived source"
            elif action == "filter":
                logical_op = "WHERE or HAVING predicate"
            elif action == "join":
                logical_op = "JOIN combination"
            elif action == "aggregate":
                logical_op = "GROUP BY and aggregation"
            elif action == "window":
                logical_op = "Window calculation"
            elif action == "sort":
                logical_op = "ORDER BY"
            elif action == "limit":
                logical_op = "LIMIT"
            elif action in {"materialize", "hash"}:
                logical_op = "Physical wrapper around child rows"
            else:
                logical_op = "Physical-only or unmapped operation"
            step = {
                "step": len(steps) + 1,
                "node_id": node_id,
                "parent_id": parent_id,
                "child_ids": [str(child.get("id") or "") for child in child_nodes],
                "layer": "physical execution-plan operator",
                "operator": operator,
                "op": action,
                "logical_op": logical_op,
                "mapping_note": "Operator-family mapping is an inference; MySQL can merge, reorder, or combine logical SQL operations.",
                "label": f"{operator}: {node.get('details') or ''}".rstrip(": "),
                "condition": str(node.get("details") or "") if action == "filter" else "",
                "input_branches": branches,
                "output_schema": output_schema,
                "input_rids": input_rids,
                "output_rids": output_rids,
                "event_rids": input_rids if action in {"filter", "limit", "sort"} else output_rids,
                "matched_pairs": matched_pairs,
                "branch_join": action == "join",
                "matched_input_rids": sorted({rid for pair in matched_pairs for rid in pair if rid}) if action == "join" else [],
                "row_results": row_results,
                "row_data": row_data,
                "evaluable": evaluable,
                "exactness": exactness,
                "est_rows": float(node.get("estimated_rows") or 0) * float(node.get("loops") or 1),
                "actual_rows": float(physical_rows or 0),
                "loops": node.get("loops"),
                "table_row_count": table_count,
                "never_executed": never_executed,
                "note": note,
            }
            steps.append(step)
            output_by_node[node_id] = list(output_rids)
            output_schema_by_node[node_id] = list(output_schema)
            output_exactness_by_node[node_id] = exactness
            return step

        visit(plan_tree)

    if final_rows is not None and steps:
        root_step = next((step for step in steps if step["node_id"] == str(plan_tree.get("id") or "")), steps[-1])
        if root_step["op"] == "scan":
            root_step["source_sample_rids"] = list(root_step["output_rids"])
        final_rids = []
        for index, row in enumerate(final_rows[:10], 1):
            rid = f"query_result#{index}"
            records[rid] = {
                "table": "Query result",
                "schema": None,
                "alias": "result",
                "source_aliases": [],
                "qualified_values": {},
                "qualifier_aliases": {},
                "values": {str(column): value for column, value in row.items()},
                "sample_origin": "actual original query result",
            }
            final_rids.append(rid)
        root_step["output_rids"] = final_rids
        root_step["event_rids"] = final_rids
        root_step["row_data"].update({rid: records[rid] for rid in final_rids})
        root_step["exactness"] = "actual_query_result"
        root_step["evaluable"] = True
        root_step["note"] = f"Final output comes from the original query execution ({len(final_rids)} sampled result rows)."
        root_step["logical_op"] = "Final SELECT result"
        root_step["op"] = "result"
    return steps
