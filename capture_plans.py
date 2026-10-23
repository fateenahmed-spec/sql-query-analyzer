import json
from pathlib import Path

from db import run_explain_analyze

QUERIES = {
    "01_full_scan": "SELECT * FROM orders WHERE amount > 500",
    "02_index_lookup": "SELECT * FROM orders WHERE customer_id = 7",
    "03_join_indexed": "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.amount > 900",
    "04_group_by": "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id",
    "05_sort_limit": "SELECT * FROM orders ORDER BY amount DESC LIMIT 10",
    "06_subquery": "SELECT * FROM customers WHERE id IN (SELECT customer_id FROM orders WHERE amount > 990)",
    "07_dead_end": "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.amount > 5000",
    "08_hash_join": "SELECT o.id, c.name FROM orders o JOIN customers c ON c.city = o.status",
    "09_full_pipeline": "SELECT c.city, SUM(o.amount) AS total FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.amount > 100 GROUP BY c.city ORDER BY total DESC",
}

def main():
    dest = Path(__file__).resolve().parent / "tests/fixtures/plans"
    dest.mkdir(parents=True, exist_ok=True)
    for i, (name, query) in enumerate(QUERIES.items(), 1):
        result = run_explain_analyze(query)
        (dest / f"{i:02d}_{name.split('_', 1)[1]}.txt").write_text(result["raw_plan"].replace("\r\n", "\n") + "\n", encoding="utf-8", newline="\n")
    (Path(__file__).resolve().parent / "tests/fixtures/queries.json").write_text(json.dumps(QUERIES, indent=2) + "\n", encoding="utf-8", newline="\n")

if __name__ == '__main__': main()
