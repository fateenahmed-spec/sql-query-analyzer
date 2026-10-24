import requests

sql = "SELECT id, name FROM customers WHERE city = 'Mumbai' LIMIT 5"
data = requests.post("http://127.0.0.1:8000/api/run-live", json={"query": sql}).json()

for k, v in data.items():
    size = len(v) if hasattr(v, "__len__") else v
    print(f"{k:15} {type(v).__name__:6} {size}")
print("warnings:", data.get("warnings"))
