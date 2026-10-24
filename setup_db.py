import os
import random
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

def main():
    root_password = os.getenv("MYSQL_ROOT_PASSWORD")
    if not root_password or root_password == "your_password":
        raise RuntimeError("Set MYSQL_ROOT_PASSWORD before running setup_db.py")
    env_path = ROOT / ".env"
    if not env_path.exists():
        db_password = secrets.token_urlsafe(16)
        env_path.write_text(f"DB_HOST=127.0.0.1\nDB_USER=analyzer_ro\nDB_PASS={db_password}\nDB_NAME=sql_analyzer_test\n", encoding="utf-8")
    else:
        db_password = os.getenv("DB_PASS", "")
    conn = mysql.connector.connect(host="127.0.0.1", user="root", password=root_password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS sql_analyzer_test")
    cur.execute("CREATE DATABASE IF NOT EXISTS analyzer_uploads")
    for host in ("localhost", "127.0.0.1"):
        cur.execute(f"CREATE USER IF NOT EXISTS 'analyzer_ro'@'{host}' IDENTIFIED BY %s", (db_password,))
        cur.execute(f"ALTER USER 'analyzer_ro'@'{host}' IDENTIFIED BY %s", (db_password,))
        cur.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'analyzer_ro'@'{host}'")
        cur.execute(f"GRANT SELECT ON sql_analyzer_test.* TO 'analyzer_ro'@'{host}'")
        cur.execute(f"GRANT SELECT, INSERT, CREATE, DROP ON analyzer_uploads.* TO 'analyzer_ro'@'{host}'")
    cur.execute("USE sql_analyzer_test")
    cur.execute("CREATE TABLE IF NOT EXISTS customers (id INT PRIMARY KEY, name VARCHAR(100), city VARCHAR(50))")
    cur.execute("CREATE TABLE IF NOT EXISTS orders (id INT PRIMARY KEY, customer_id INT, amount DECIMAL(10,2), status VARCHAR(20), created_at DATETIME, INDEX idx_customer (customer_id))")
    cur.execute("SELECT COUNT(*) FROM customers"); nc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders"); no = cur.fetchone()[0]
    if (nc, no) != (500, 20000):
        cur.execute("SET FOREIGN_KEY_CHECKS=0"); cur.execute("TRUNCATE TABLE orders"); cur.execute("TRUNCATE TABLE customers")
        rng = random.Random(42)
        cities = ['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Kolkata']
        cur.executemany("INSERT INTO customers VALUES (%s,%s,%s)", [(i, f'Customer {i}', rng.choice(cities)) for i in range(1, 501)])
        statuses = ['pending', 'shipped', 'delivered', 'cancelled']
        anchor = datetime(2023, 1, 1)
        cur.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)", [(i, rng.randint(1, 500), f'{rng.uniform(1,1000):.2f}', rng.choice(statuses), anchor + timedelta(seconds=rng.randint(0, 63072000))) for i in range(1, 20001)])
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
    cur.execute("SELECT COUNT(*) FROM customers"); nc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders"); no = cur.fetchone()[0]
    if (nc, no) != (500, 20000): raise RuntimeError(f"Unexpected row counts: {nc}, {no}")
    cur.execute("ANALYZE TABLE customers"); cur.fetchall()
    cur.execute("ANALYZE TABLE orders"); cur.fetchall()
    cur.close(); conn.close()

if __name__ == '__main__': main()
