import re

import sqlparse
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
        parsed = sqlparse.parse(cleaned)
        scrubbed_parts = []
        for statement in parsed:
            for token in statement.flatten():
                if token.ttype in T.Comment or token.ttype in T.Literal.String:
                    scrubbed_parts.append(" ")
                else:
                    scrubbed_parts.append(token.value)
        scrubbed = "".join(scrubbed_parts)
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
