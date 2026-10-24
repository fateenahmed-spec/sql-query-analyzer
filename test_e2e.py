"""Live MySQL and Gemini smoke checks. Run separately from pytest."""

from __future__ import annotations

import sys
import io
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

    run_csv_checks()

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


def run_csv_checks() -> None:
    uploaded_table = None
    try:
        csv_file = io.StringIO(newline="")
        csv_file.write("id,select,4 Status,created,score\n")
        csv_file.write("1,active,Ready,2025-02-01,1.5\n")
        csv_file.write("2,inactive,Waiting,2025-02-02,2\n")
        csv_file.write("3,active,Done,2025-02-03,3.25\n")
        filename = f"e2e_csv_{time.time_ns()}.csv"
        upload_response = requests.post(
            f"{BASE_URL}/api/upload-csv",
            files={"file": (filename, b"\xef\xbb\xbf" + csv_file.getvalue().encode("utf-8"), "text/csv")},
            timeout=TIMEOUT_SECONDS,
        )
        upload_data = upload_response.json()
        uploaded_table = upload_data.get("table") if upload_data.get("ok") else None
        list_response = requests.get(f"{BASE_URL}/api/uploads", timeout=TIMEOUT_SECONDS)
        list_data = list_response.json()
        listed_table = any(item.get("table") == uploaded_table for item in list_data.get("uploads", []))
        columns = upload_data.get("columns", [])
        passed = (
            upload_response.status_code == 200
            and upload_data.get("ok") is True
            and upload_data.get("qualified_name", "").startswith("analyzer_uploads.up_")
            and upload_data.get("row_count") == 3
            and list_response.status_code == 200
            and listed_table
            and [(column.get("name"), column.get("type")) for column in columns]
            == [("id", "INT"), ("select_", "TEXT"), ("col_4_status", "TEXT"), ("created", "DATE"), ("score", "DOUBLE")]
        )
        report("7. CSV upload creates a separate-schema table with inferred columns and row count", passed)
    except (requests.RequestException, ValueError) as exc:
        upload_data = {}
        report("7. CSV upload creates a separate-schema table with inferred columns and row count", False, str(exc))

    try:
        if uploaded_table:
            query = f"SELECT * FROM `analyzer_uploads`.`{uploaded_table}` ORDER BY id"
            response = post_live(query)
            data = response.json()
            passed = (
                response.status_code == 200
                and isinstance(data.get("rows"), list)
                and len(data["rows"]) == 3
                and isinstance(data.get("tree"), dict)
                and isinstance(data.get("steps"), list)
                and bool(data["steps"])
                and isinstance(data.get("source_samples"), dict)
                and bool(data["source_samples"])
            )
        else:
            passed = False
        report("8. Uploaded table query returns rows, tree, steps, and source samples", passed)
    except (requests.RequestException, ValueError) as exc:
        report("8. Uploaded table query returns rows, tree, steps, and source samples", False, str(exc))

    try:
        wrong_extension = requests.post(
            f"{BASE_URL}/api/upload-csv",
            files={"file": ("bad.txt", b"id\n1\n", "text/plain")},
            timeout=TIMEOUT_SECONDS,
        )
        oversized = requests.post(
            f"{BASE_URL}/api/upload-csv",
            files={"file": ("large.csv", b"x" * (5 * 1024 * 1024 + 1), "text/csv")},
            timeout=TIMEOUT_SECONDS,
        )
        report("9. Wrong-extension and oversized CSV uploads return HTTP 400", wrong_extension.status_code == 400 and oversized.status_code == 400)
    except requests.RequestException as exc:
        report("9. Wrong-extension and oversized CSV uploads return HTTP 400", False, str(exc))

    try:
        if uploaded_table:
            response = post_live(f"INSERT INTO analyzer_uploads.{uploaded_table} VALUES (99, 'active', 'Ready', '2025-02-04', 9.5)")
            passed = response.status_code == 400
        else:
            passed = False
        report("10. INSERT on an uploaded table is rejected by the live-query guard", passed)
    except requests.RequestException as exc:
        report("10. INSERT on an uploaded table is rejected by the live-query guard", False, str(exc))

    try:
        if uploaded_table:
            delete_response = requests.delete(f"{BASE_URL}/api/uploads/{uploaded_table}", timeout=TIMEOUT_SECONDS)
            deleted = delete_response.status_code == 200 and delete_response.json().get("ok") is True
            missing_response = post_live(f"SELECT * FROM analyzer_uploads.{uploaded_table} LIMIT 1")
            missing_data = missing_response.json()
            query_errors = missing_response.status_code >= 400 or (
                missing_response.status_code == 200 and missing_data.get("ok") is False and bool(missing_data.get("error"))
            )
            list_response = requests.get(f"{BASE_URL}/api/uploads", timeout=TIMEOUT_SECONDS)
            list_data = list_response.json()
            no_longer_listed = not any(item.get("table") == uploaded_table for item in list_data.get("uploads", []))
            passed = deleted and query_errors and list_response.status_code == 200 and no_longer_listed
            if deleted:
                uploaded_table = None
        else:
            passed = False
        report("11. Deleted upload table is removed and can no longer be queried", passed)
    except (requests.RequestException, ValueError) as exc:
        report("11. Deleted upload table is removed and can no longer be queried", False, str(exc))
    finally:
        if uploaded_table:
            try:
                requests.delete(f"{BASE_URL}/api/uploads/{uploaded_table}", timeout=TIMEOUT_SECONDS)
            except requests.RequestException:
                pass


if __name__ == "__main__":
    sys.exit(main())
