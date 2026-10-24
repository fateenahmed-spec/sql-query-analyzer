import os
import re

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis
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
