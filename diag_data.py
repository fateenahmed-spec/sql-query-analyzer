import requests

def run(sql):
    d = requests.post("http://127.0.0.1:8000/api/run-live", json={"query": sql}).json()
    print(sql)
    print("  ->", d.get("rows", d.get("error")))

run("SELECT COUNT(*) AS n FROM customers")
run("SELECT city, COUNT(*) AS n FROM customers GROUP BY city LIMIT 20")
