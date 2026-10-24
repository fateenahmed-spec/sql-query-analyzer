import requests

csv = "id,name,city\n1,Asha,Pune\n2,Ravi,Hassan\n3,Meena,Pune\n"
with open("sample_upload.csv", "w", encoding="utf-8") as f:
    f.write(csv)

with open("sample_upload.csv", "rb") as f:
    r = requests.post(
        "http://127.0.0.1:8000/api/upload-csv",
        files={"file": ("sample_upload.csv", f, "text/csv")},
        data={"table_name": "sample_test", "overwrite": "true"},
    )
print("STATUS:", r.status_code)
print("RESPONSE:", r.text)
