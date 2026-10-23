"""Rule-guided explanations for demo SQL plans."""


def get_ai_explanation(query_text: str, warnings: list[dict]) -> dict:
    summary = "This query is optimized with clean execution paths and no major bottlenecks."
    suggestions = []
    codes = {warning.get("code") for warning in warnings}

    if "FULL_SCAN" in codes or "INNER_SCAN_IN_LOOP" in codes:
        summary = "This query performs full table scans, reading unindexed disk blocks."
        suggestions.append("Consider adding composite indexes on filtered join/where columns.")

    if "LARGE_SORT" in codes:
        suggestions.append("Avoid sorting large unindexed datasets without an explicit LIMIT.")

    if "TEMP_TABLE" in codes:
        suggestions.append("Query uses implicit temporary tables for grouping/aggregation; optimize GROUP BY keys.")

    return {"summary": summary, "suggestions": suggestions}
