"""Live MySQL and Gemini smoke checks. Run separately from pytest."""

from __future__ import annotations

import sys
import time

import requests


BASE_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 30
failures = 0


def report(label: str, passed: bool, detail: str = "") -> None:
    global failures
    marker = "âœ…" if passed else "âŒ"
    suffix = f" â€” {detail}" if detail else ""
    print(f"{marker} {label}{suffix}")
    if not passed:
        failures += 1


def post_live(query: str) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/api/run-live",
        json={"query": query},
        timeout=TIMEOUT_SECONDS,
    )


def main() -> int:
    try:
        health = requests.get(f"{BASE_URL}/api/health", timeout=3)
        if health.status_code != 200:
            raise requests.RequestException(f"health endpoint returned {health.status_code}")
    except requests.RequestException:
        print("âŒ Server is not reachable. Start it with: uvicorn main:app --reload")
        return 1

    query = "SELECT id, name FROM customers WHERE city = 'Pune' LIMIT 5"
    first_response = None
    first_data = None
    try:
        first_response = post_live(query)
        first_data = first_response.json()
        passed = (
            first_response.status_code == 200
            and isinstance(first_data, dict)
            and all(key in first_data for key in ("tree", "steps", "warnings", "source_samples"))
            and isinstance(first_data.get("rows"), list)
            and len(first_data["rows"]) > 0
        )
        report("1. Live query returns real rows, plan, steps, warnings, and source samples", passed)
    except (requests.RequestException, ValueError) as exc:
        report("1. Live query returns real rows, plan, steps, warnings, and source samples", False, str(exc))

    try:
        unsafe_response = post_live("DROP TABLE customers")
        unsafe_ok = unsafe_response.status_code == 400
        follow_up = post_live("SELECT id, name FROM customers WHERE city = 'Mumbai' LIMIT 1")
        follow_up_ok = follow_up.status_code == 200 and follow_up.json().get("ok") is not False
        report("2. Write query is rejected and a later read still works", unsafe_ok and follow_up_ok)
    except (requests.RequestException, ValueError) as exc:
        report("2. Write query is rejected and a later read still works", False, str(exc))

    error_data = None
    try:
        error_response = post_live("SELECT missing_column_for_e2e FROM customers LIMIT 1")
        error_data = error_response.json()
        passed = (
            error_response.status_code == 200
            and error_data.get("ok") is False
            and bool((error_data.get("error") or {}).get("message"))
        )
        report("3. Invalid column is returned as a MySQL error result", passed)
    except (requests.RequestException, ValueError) as exc:
        report("3. Invalid column is returned as a MySQL error result", False, str(exc))

    try:
        empty_response = post_live("SELECT id, name FROM customers WHERE city = 'Atlantis' LIMIT 5")
        empty_data = empty_response.json()
        found_dead_end = any(node.get("dead_end") for node in walk(empty_data.get("tree", {})))
        passed = empty_response.status_code == 200 and not empty_data.get("rows") and found_dead_end
        report("4. Empty result returns zero rows and marks a dead-end node", passed)
    except (requests.RequestException, ValueError) as exc:
        report("4. Empty result returns zero rows and marks a dead-end node", False, str(exc))

    ai_first = None
    try:
        ai_response = requests.post(
            f"{BASE_URL}/api/ai",
            json={
                "query": query,
                "warnings": (first_data or {}).get("warnings", []),
                "tree": (first_data or {}).get("tree"),
            },
            timeout=25,
        )
        ai_first = ai_response.json()
        if ai_first.get("source") == "fallback":
            print(f"Gemini error: {ai_first.get('error', 'No error details returned.')}")
        if ai_first.get("source") == "fallback" and any(
            word in str(ai_first.get("note", "")).casefold() for word in ("429", "quota", "rate-limit", "rate limit")
        ):
            report("5. Gemini returns a real explanation", False, "Gemini rate-limited or quota exhausted")
            print("âš  Gemini rate-limited, wait 60s and rerun")
            return 1
        passed = (
            ai_response.status_code == 200
            and ai_first.get("source") == "gemini"
            and bool(ai_first.get("summary"))
            and isinstance(ai_first.get("suggestions"), list)
        )
        report("5. Gemini returns a real explanation", passed, "fallback is not accepted" if not passed else "")
    except (requests.RequestException, ValueError) as exc:
        report("5. Gemini returns a real explanation", False, str(exc))

    time.sleep(7)
    try:
        ai_error_response = requests.post(
            f"{BASE_URL}/api/ai",
            json={
                "query": (error_data or {}).get("query", "SELECT missing_column_for_e2e FROM customers LIMIT 1"),
                "warnings": [],
                "error": (error_data or {}).get("error", {"code": "1054", "message": "Unknown column"}),
            },
            timeout=25,
        )
        ai_error = ai_error_response.json()
        passed = ai_error_response.status_code == 200 and isinstance(ai_error.get("fix"), str) and bool(ai_error["fix"].strip())
        report("6. AI response for a MySQL error includes a suggested fix", passed)
    except (requests.RequestException, ValueError) as exc:
        report("6. AI response for a MySQL error includes a suggested fix", False, str(exc))

    if failures:
        print(f"âŒ E2E TEST FAILED: {failures} check(s) failed.")
        return 1
    print("âœ… E2E TEST PASSED: Live DB and real Gemini AI are fully functional.")
    return 0


def walk(tree: dict):
    if not isinstance(tree, dict):
        return
    yield tree
    for child in tree.get("children", []):
        yield from walk(child)


if __name__ == "__main__":
    sys.exit(main())
