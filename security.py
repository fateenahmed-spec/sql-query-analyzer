import os
import re

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis, Token
from sqlparse import tokens as T


class UnsafeQueryError(Exception):
    pass


_BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|REPLACE)\b",
    re.IGNORECASE,
)
_BLOCKED_PATTERNS = re.compile(
    r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b|"
    r"\bFOR\s+(?:UPDATE|SHARE)\b|"
    r"\bLOCK\s+IN\s+SHARE\s+MODE\b|"
    r"\b(?:SLEEP|BENCHMARK|LOAD_FILE|GET_LOCK|MASTER_POS_WAIT|SOURCE_POS_WAIT|WAIT_FOR_EXECUTED_GTID_SET)\s*\(",
    re.IGNORECASE,
)
def _scrub_literals_and_comments(sql: str) -> str:
    parsed = sqlparse.parse(sql)
    scrubbed_parts = []
    for statement in parsed:
        for token in statement.flatten():
            if token.ttype in T.Comment or token.ttype in T.Literal.String:
                scrubbed_parts.append(" ")
            else:
                scrubbed_parts.append(token.value)
    return "".join(scrubbed_parts)


def _source_schemas(sql: str) -> list[tuple[str | None, str]]:
    references = []

    def visit(token_list) -> None:
        expect_source = False
        for token in token_list.tokens:
            if token.ttype in T.Keyword and (
                token.normalized.upper() == "FROM" or token.normalized.upper().endswith(" JOIN")
                or token.normalized.upper() == "JOIN"
            ):
                expect_source = True
                continue
            if expect_source and (token.is_whitespace or token.ttype in T.Comment):
                continue
            if expect_source and token.ttype in T.Keyword and token.normalized.upper() in {"LATERAL", "ONLY"}:
                continue
            if expect_source and isinstance(token, IdentifierList):
                for identifier in token.get_identifiers():
                    if isinstance(identifier, Identifier):
                        references.append((identifier.get_parent_name(), identifier.get_real_name() or ""))
                        visit(identifier)
                expect_source = False
            elif expect_source and isinstance(token, Identifier):
                references.append((token.get_parent_name(), token.get_real_name() or ""))
                visit(token)
                expect_source = False
            elif expect_source and isinstance(token, Parenthesis):
                visit(token)
                expect_source = False
            elif token.is_group:
                visit(token)

    for statement in sqlparse.parse(sql):
        visit(statement)
    return [(schema, table) for schema, table in references if table]


def _cte_names(token_list) -> set[str]:
    """Return CTE names declared at this query level."""
    tokens = token_list.tokens
    for index, token in enumerate(tokens):
        if token.ttype not in T.Keyword or token.normalized.upper() != "WITH":
            continue
        index += 1
        while index < len(tokens) and (tokens[index].is_whitespace or tokens[index].ttype in T.Comment):
            index += 1
        if index < len(tokens) and tokens[index].normalized.upper() == "RECURSIVE":
            index += 1
            while index < len(tokens) and tokens[index].is_whitespace:
                index += 1
        if index >= len(tokens):
            return set()
        definitions = tokens[index]
        if isinstance(definitions, IdentifierList):
            definitions = definitions.get_identifiers()
        elif isinstance(definitions, Identifier):
            definitions = [definitions]
        else:
            return set()
        return {
            name.casefold()
            for definition in definitions
            if isinstance(definition, Identifier)
            and (name := definition.get_name())
        }
    return set()


def _source_identifiers(token_list, inherited_ctes: set[str] | None = None):
    """Yield table identifiers from FROM/JOIN clauses, including nested queries."""
    cte_names = set(inherited_ctes or ()) | _cte_names(token_list)
    expect_source = False
    for token in token_list.tokens:
        if token.ttype in T.Keyword and (
            token.normalized.upper() == "FROM"
            or token.normalized.upper() == "JOIN"
            or token.normalized.upper().endswith(" JOIN")
        ):
            expect_source = True
            continue

        if expect_source and (token.is_whitespace or token.ttype in T.Comment):
            continue
        if expect_source and token.ttype in T.Keyword and token.normalized.upper() in {"LATERAL", "ONLY"}:
            continue

        if expect_source and isinstance(token, IdentifierList):
            for identifier in token.get_identifiers():
                if isinstance(identifier, Identifier):
                    if not any(isinstance(child, Parenthesis) for child in identifier.tokens):
                        yield identifier, cte_names
                    yield from _source_identifiers(identifier, cte_names)
            expect_source = False
            continue

        if expect_source and isinstance(token, Identifier):
            if not any(isinstance(child, Parenthesis) for child in token.tokens):
                yield token, cte_names
            yield from _source_identifiers(token, cte_names)
            expect_source = False
            continue

        if expect_source and isinstance(token, Parenthesis):
            yield from _source_identifiers(token, cte_names)
            expect_source = False
            continue

        if token.is_group:
            yield from _source_identifiers(token, cte_names)


def resolve_uploaded_table_references(
    sql: str,
    upload_tables: list[str] | tuple[str, ...] | set[str],
    default_tables: list[str] | tuple[str, ...] | set[str],
) -> str:
    """Qualify bare uploaded table names using parsed source identifiers only."""
    cleaned = validate_live_query(sql)
    statements = sqlparse.parse(cleaned)
    if len(statements) != 1:
        raise UnsafeQueryError("Multiple statements are not allowed")

    uploads_by_name: dict[str, set[str]] = {}
    for physical_name in upload_tables:
        physical = str(physical_name)
        if not re.fullmatch(r"up_[a-z0-9_]{1,61}", physical, re.IGNORECASE) or physical.casefold() == "up_temp_clone":
            continue
        uploads_by_name.setdefault(physical.casefold(), set()).add(physical)
        uploads_by_name.setdefault(physical[3:].casefold(), set()).add(physical)
    default_names = {str(name).casefold() for name in default_tables}

    for identifier, cte_names in _source_identifiers(statements[0]):
        if identifier.get_parent_name() is not None:
            continue
        table_name = (identifier.get_real_name() or "").strip("`\"[]")
        lookup_name = table_name.casefold()
        if not lookup_name or lookup_name in cte_names:
            continue
        upload_matches = uploads_by_name.get(lookup_name, set())
        if not upload_matches:
            continue
        if len(upload_matches) > 1:
            raise UnsafeQueryError(
                f"Ambiguous table name '{table_name}': it matches multiple uploaded tables. "
                "Use a schema-qualified table name to choose one."
            )
        if lookup_name in default_names:
            raise UnsafeQueryError(
                f"Ambiguous table name '{table_name}': it matches both a default-schema table and an uploaded table. "
                "Use a schema-qualified table name to choose one."
            )

        physical_name = next(iter(upload_matches))
        first_name_token = next((token for token in identifier.tokens if token.ttype in T.Name), None)
        if first_name_token is None:
            continue
        replacement = Token(T.Name, f"`analyzer_uploads`.`{physical_name}`")
        replacement.parent = identifier
        identifier.tokens[identifier.tokens.index(first_name_token)] = replacement

    return str(statements[0])


def validate_query(sql: str) -> str:
    cleaned = sql.strip()
    cleaned = re.sub(r"^EXPLAIN(?:\s+ANALYZE)?\b", "", cleaned, count=1, flags=re.IGNORECASE).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        raise UnsafeQueryError("Empty query")

    if any(marker in cleaned for marker in ("/*!", "/*+", "/*M!", "#")):
        raise UnsafeQueryError("Unsupported comment syntax")
    if re.search(r"--(?:\S|$)", cleaned):
        raise UnsafeQueryError("Invalid SQL comment syntax")

    try:
        scrubbed = _scrub_literals_and_comments(cleaned)
    except Exception as exc:
        raise UnsafeQueryError("Parse error") from exc

    if ";" in scrubbed:
        raise UnsafeQueryError("Multiple statements are not allowed")
    parsed_scrubbed = sqlparse.parse(scrubbed)
    if not parsed_scrubbed:
        raise UnsafeQueryError("Empty query or comment-only input")

    first = next((t.value.upper() for t in parsed_scrubbed[0].flatten()
                  if t.ttype in T.Keyword or t.ttype in T.DML), "")
    if not first:
        raise UnsafeQueryError("Could not identify a valid SELECT or WITH statement")
    if first not in {"SELECT", "WITH"}:
        raise UnsafeQueryError("Only SELECT queries are allowed")
    # Function forms INSERT(...) and REPLACE(...) are safe scalar functions.
    for match in _BLOCKED_KEYWORDS.finditer(scrubbed):
        word = match.group(1).upper()
        tail = scrubbed[match.end():]
        if word in {"INSERT", "REPLACE"} and re.match(r"\s*\(", tail) and not re.match(r"\s+INTO\b", tail, re.I):
            continue
        raise UnsafeQueryError(f"Blocked keyword: {word}")
    if _BLOCKED_PATTERNS.search(scrubbed):
        raise UnsafeQueryError("Blocked query pattern")
    return cleaned


def validate_live_query(sql: str) -> str:
    """Validate a live read and restrict qualified references to app schemas."""
    cleaned = validate_query(sql)
    scrubbed = _scrub_literals_and_comments(cleaned)
    main_schema = (os.getenv("DB_NAME") or "sql_analyzer_test").casefold()
    allowed_main_schemas = {main_schema, "sql_analyzer_test"}
    for source_schema, source_table in _source_schemas(scrubbed):
        if source_schema is None:
            continue
        schema = source_schema.strip("`").casefold()
        table = source_table.strip("`").casefold()
        if schema in allowed_main_schemas:
            continue
        if schema == "analyzer_uploads" and table.startswith("up_"):
            continue
        raise UnsafeQueryError("Only app database tables and analyzer_uploads.up_* tables may be queried")
    return cleaned
