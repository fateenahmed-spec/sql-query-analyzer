import requests

T, O = "`analyzer_uploads`.`up_employees`", "`analyzer_uploads`.`up_orders`"
Q = [
 ("simple filter",      f"SELECT * FROM {T} WHERE department = 'Sales'"),
 ("built-in customers", "SELECT id, name FROM customers WHERE city IN ('Pune', 'Delhi') ORDER BY id LIMIT 8"),
 ("filter+sort+limit",  f"SELECT full_name, department, salary FROM {T} WHERE department IN ('Engineering', 'Finance') AND salary > 90000 AND active = 'Y' ORDER BY salary DESC LIMIT 8"),
 ("join USING",         f"SELECT full_name, order_id, amount FROM {T} JOIN {O} USING (employee_id) WHERE city = 'Pune' AND status = 'delivered' ORDER BY amount DESC LIMIT 10"),
 ("window CTE",         f"WITH ranked AS (SELECT full_name, department, salary, ROW_NUMBER() OVER (PARTITION BY department ORDER BY salary DESC) AS rn FROM {T}) SELECT full_name, department, salary, rn FROM ranked WHERE rn <= 3"),
 ("self-join aliased",  f"SELECT a.full_name, b.full_name FROM {T} a JOIN {T} b ON a.department = b.department AND a.salary > b.salary * 1.5 WHERE a.city = 'Pune'"),
]
fails = 0
for label, sql in Q:
    d = requests.post("http://127.0.0.1:8000/api/run-live", json={"query": sql}).json()
    flow = d.get("row_flow") or []
    rec = d.get("row_records") or {}
    mapped = sum(1 for n in flow if n.get("table_row_count", 0) > 0)
    evald = sum(1 for n in flow if n.get("evaluable"))
    ok = len(rec) > 0
    fails += 0 if ok else 1
    print(("PASS" if ok else "FAIL"), f"{label:20} nodes={len(flow):2} mapped={mapped} evaluable={evald} row_records={len(rec)}")
print("\nFAILED:", fails, "of", len(Q))
