"""Verify local MySQL auth, seed data, upload grants, and optional cold auth."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from db import get_connection, safe_database_error_message


LOCAL_HOSTS = ("localhost", "127.0.0.1")
APP_USER = "analyzer_ro"
UPLOAD_SCHEMA = "analyzer_uploads"
failures = 0


def report(name: str, passed: bool, detail: str, likely_cause: str = "") -> None:
    global failures
    status = "PASS" if passed else "FAIL"
    line = f"{status}: {name} — {detail}"
    if not passed and likely_cause:
        line += f"; likely cause: {likely_cause}"
    print(line)
    if not passed:
        failures += 1


def likely_cause(exc: Exception) -> str:
    errno = getattr(exc, "errno", None)
    if errno == 1045:
        return "app/root password, account host entry, or TLS authentication configuration"
    if errno in {1044, 1142, 1143}:
        return "the account is missing the required schema privilege"
    if errno in {2002, 2003, 2005}:
        return "MySQL is stopped, DB_PORT is wrong, or the local listener is unavailable"
    if isinstance(exc, requests.RequestException):
        return "start the local app with uvicorn main:app --reload"
    return "inspect the local MySQL error and configuration"


def run_check(name: str, action, cause: str) -> object | None:
    try:
        detail = action()
        report(name, True, str(detail))
        return detail
    except Exception as exc:
        errno = getattr(exc, "errno", None)
        safe_message = safe_database_error_message(exc)
        detail = f"{type(exc).__name__}" + (f" {errno}" if errno is not None else "")
        if safe_message:
            detail += f": {safe_message}"
        report(name, False, detail, cause or likely_cause(exc))
        return None


def open_admin():
    root_password = os.getenv("MYSQL_ROOT_PASSWORD", "")
    if not root_password:
        raise RuntimeError("MYSQL_ROOT_PASSWORD is not set")
    return get_connection(
        include_database=False,
        user=os.getenv("DB_ADMIN_USER") or "root",
        password=root_password,
        host=os.getenv("DB_HOST") or "127.0.0.1",
    )


def open_app(host: str, include_database: bool = True):
    password = os.getenv("DB_PASS", "")
    if not password:
        raise RuntimeError("DB_PASS is not set")
    return get_connection(
        include_database=include_database,
        user=APP_USER,
        password=password,
        host=host,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cold-start-test",
        action="store_true",
        help="flush MySQL privileges, then verify app TLS login and the local app health endpoint",
    )
    args = parser.parse_args()

    db_host = (os.getenv("DB_HOST") or "127.0.0.1").strip().casefold()
    if db_host not in LOCAL_HOSTS:
        print("FAIL: local-only guard — DB_HOST must be 127.0.0.1 or localhost; no DB connection attempted")
        return 1

    if run_check("root TLS login", lambda: _root_login(), "check MYSQL_ROOT_PASSWORD and local root access") is None:
        return 1

    if args.cold_start_test:
        def flush_auth_cache():
            conn = open_admin()
            try:
                with closing(conn.cursor()) as cursor:
                    cursor.execute("FLUSH PRIVILEGES")
                return "FLUSH PRIVILEGES succeeded over local TLS"
            finally:
                conn.close()

        if run_check("cold-auth cache flush", flush_auth_cache, "root account lacks FLUSH privilege") is None:
            return 1

        def health_check():
            base_url = os.getenv("SQL_ANALYZER_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
            if (urlsplit(base_url).hostname or "").casefold() not in LOCAL_HOSTS:
                raise RuntimeError("SQL_ANALYZER_BASE_URL must point to localhost or 127.0.0.1")
            response = requests.get(f"{base_url}/api/health", timeout=8)
            body = response.json()
            is_up = body.get("db") == "up" or body.get("database") == "connected"
            if response.status_code != 200 or not is_up:
                raise RuntimeError(f"local health returned HTTP {response.status_code} with DB unavailable")
            return "HTTP 200; app database connection is healthy after FLUSH PRIVILEGES"

        run_check("app /api/health after cold-auth flush", health_check, "ensure uvicorn is running and local TLS is enabled")

    for host in LOCAL_HOSTS:
        def app_login(host=host):
            conn = open_app(host)
            try:
                with closing(conn.cursor()) as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                return f"TLS login succeeded for {host}"
            finally:
                conn.close()

        run_check(f"app user TLS login at {host}", app_login, f"check the {host} account entry and DB_PASS")

    def customer_count():
        conn = open_app(os.getenv("DB_HOST") or "127.0.0.1")
        try:
            with closing(conn.cursor()) as cursor:
                cursor.execute("SELECT COUNT(*) FROM customers")
                count = cursor.fetchone()[0]
            if count != 500:
                raise RuntimeError(f"expected 500 customers rows, found {count}")
            return f"customers row count is {count}"
        finally:
            conn.close()

    run_check("customers table access and row count", customer_count, "run python setup_db.py to create and seed the local database")

    def upload_write_access():
        table_name = f"check_db_{uuid.uuid4().hex[:12]}"
        conn = open_app(os.getenv("DB_HOST") or "127.0.0.1", include_database=False)
        try:
            with closing(conn.cursor()) as cursor:
                cursor.execute(f"CREATE TABLE `{UPLOAD_SCHEMA}`.`{table_name}` (id INT PRIMARY KEY)")
                cursor.execute(f"INSERT INTO `{UPLOAD_SCHEMA}`.`{table_name}` (id) VALUES (1)")
                cursor.execute(f"SELECT COUNT(*) FROM `{UPLOAD_SCHEMA}`.`{table_name}`")
                count = cursor.fetchone()[0]
                if count != 1:
                    raise RuntimeError("upload schema test row was not readable")
                cursor.execute(f"DROP TABLE `{UPLOAD_SCHEMA}`.`{table_name}`")
            return "CREATE, INSERT, SELECT, and DROP succeeded in analyzer_uploads"
        finally:
            try:
                with closing(conn.cursor()) as cursor:
                    cursor.execute(f"DROP TABLE IF EXISTS `{UPLOAD_SCHEMA}`.`{table_name}`")
            finally:
                conn.close()

    run_check("analyzer_uploads write access", upload_write_access, "setup_db.py must grant SELECT, INSERT, CREATE, and DROP on analyzer_uploads.*")

    if failures:
        print(f"RESULT: FAIL ({failures} check(s) failed)")
        return 1
    print("RESULT: PASS (all local database checks passed)")
    return 0


def _root_login() -> str:
    conn = open_admin()
    try:
        with closing(conn.cursor()) as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "local TLS login succeeded"
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
