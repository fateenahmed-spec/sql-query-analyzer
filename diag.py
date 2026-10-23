import os, socket, threading, urllib.request
from dotenv import load_dotenv
load_dotenv()

url = "https://generativelanguage.googleapis.com/v1beta/models"
key = os.environ.get("GEMINI_API_KEY", "")

def call(tag):
    try:
        req = urllib.request.Request(url, headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=10) as r:
            print(tag, "OK", r.status)
    except Exception as e:
        print(tag, "FAIL", type(e).__name__, e)

call("main thread")
t = threading.Thread(target=call, args=("worker thread",)); t.start(); t.join()
print("proxy env:", {k: v for k, v in os.environ.items() if "PROXY" in k.upper()})

for port in (8000, 8765):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port)); print("bind", port, "OK")
    except OSError as e:
        print("bind", port, "FAIL", e)
    finally:
        s.close()