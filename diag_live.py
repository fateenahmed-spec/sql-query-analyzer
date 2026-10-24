import requests

sql = "SELECT id, name FROM customers WHERE city = 'Mumbai' LIMIT 5"
resp = requests.post("http://127.0.0.1:8000/api/run-live", json={"query": sql})
data = resp.json()

print("status:", resp.status_code)
print("ok:", data.get("ok"))
print("error:", data.get("error"))
print("keys:", list(data.keys()))
