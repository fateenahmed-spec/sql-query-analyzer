"""Idempotently provision the local demo database and restricted app user."""

from __future__ import annotations

import os
import random
import re
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from db import get_connection, safe_database_error_message


APP_USER = "analyzer_ro"
UPLOAD_SCHEMA = "analyzer_uploads"
LOCAL_HOSTS = ("localhost", "127.0.0.1")


def _identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise RuntimeError(f"{label} must contain only letters, digits, and underscores.")
    return f"`{value}`"


def main() -> None:
    db_host = (os.getenv("DB_HOST") or "127.0.0.1").strip()
    if db_host.casefold() not in LOCAL_HOSTS:
        raise RuntimeError("Refusing setup_db.py: DB_HOST must be 127.0.0.1 or localhost.")

    root_password = os.getenv("MYSQL_ROOT_PASSWORD", "")
    db_password = os.getenv("DB_PASS", "")
    if not root_password:
        raise RuntimeError("Set MYSQL_ROOT_PASSWORD before running setup_db.py.")
    if not db_password:
        raise RuntimeError("Set DB_PASS before running setup_db.py.")

    database_name = os.getenv("DB_NAME") or "sql_analyzer_test"
    quoted_database = _identifier(database_name, "DB_NAME")
    admin_user = os.getenv("DB_ADMIN_USER") or "root"

    conn = get_connection(
        include_database=False,
        user=admin_user,
        password=root_password,
        host=db_host,
    )
    try:
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {quoted_database}")
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{UPLOAD_SCHEMA}`")

            for host in LOCAL_HOSTS:
                cur.execute(
                    f"CREATE USER IF NOT EXISTS '{APP_USER}'@'{host}' IDENTIFIED BY %s",
                    (db_password,),
                )
                # Reset the password on every run without changing the auth plugin.
                cur.execute(
                    f"ALTER USER '{APP_USER}'@'{host}' IDENTIFIED BY %s",
                    (db_password,),
                )
                cur.execute(f"ALTER USER '{APP_USER}'@'{host}' ACCOUNT UNLOCK")
                cur.execute(f"ALTER USER '{APP_USER}'@'{host}' REQUIRE NONE")
                cur.execute(
                    f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{APP_USER}'@'{host}'"
                )
                cur.execute(f"GRANT SELECT ON {quoted_database}.* TO '{APP_USER}'@'{host}'")
                cur.execute(
                    f"GRANT SELECT, INSERT, CREATE, DROP ON `{UPLOAD_SCHEMA}`.* "
                    f"TO '{APP_USER}'@'{host}'"
                )

            cur.execute(f"USE {quoted_database}")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS customers "
                "(id INT PRIMARY KEY, name VARCHAR(100), city VARCHAR(50))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS orders "
                "(id INT PRIMARY KEY, customer_id INT, amount DECIMAL(10,2), "
                "status VARCHAR(20), created_at DATETIME, INDEX idx_customer (customer_id))"
            )
            cur.execute("SELECT COUNT(*) FROM customers")
            customer_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM orders")
            order_count = cur.fetchone()[0]

            if (customer_count, order_count) != (500, 20000):
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
                cur.execute("TRUNCATE TABLE orders")
                cur.execute("TRUNCATE TABLE customers")
                rng = random.Random(42)
                cities = ["Mumbai", "Delhi", "Bangalore", "Chennai", "Kolkata"]
                cur.executemany(
                    "INSERT INTO customers VALUES (%s,%s,%s)",
                    [(i, f"Customer {i}", rng.choice(cities)) for i in range(1, 501)],
                )
                statuses = ["pending", "shipped", "delivered", "cancelled"]
                anchor = datetime(2023, 1, 1)
                cur.executemany(
                    "INSERT INTO orders VALUES (%s,%s,%s,%s,%s)",
                    [
                        (
                            i,
                            rng.randint(1, 500),
                            f"{rng.uniform(1, 1000):.2f}",
                            rng.choice(statuses),
                            anchor + timedelta(seconds=rng.randint(0, 63072000)),
                        )
                        for i in range(1, 20001)
                    ],
                )
                cur.execute("SET FOREIGN_KEY_CHECKS=1")

            cur.execute("SELECT COUNT(*) FROM customers")
            customer_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM orders")
            order_count = cur.fetchone()[0]
            if (customer_count, order_count) != (500, 20000):
                raise RuntimeError("Seed table row counts do not match expected totals.")
            cur.execute("ANALYZE TABLE customers")
            cur.fetchall()
            cur.execute("ANALYZE TABLE orders")
            cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()

    for host in LOCAL_HOSTS:
        verify = get_connection(
            include_database=True,
            user=APP_USER,
            password=db_password,
            host=host,
        )
        try:
            with closing(verify.cursor()) as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            print(f"PASS: app user TLS login on {host}")
        finally:
            verify.close()

    print(
        "PASS: local database and analyzer_uploads schema are ready; "
        f"customers={customer_count}, orders={order_count}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: setup_db.py ({type(exc).__name__}): {safe_database_error_message(exc)}")
        sys.exit(1)
