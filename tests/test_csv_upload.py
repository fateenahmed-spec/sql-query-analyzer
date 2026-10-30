from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

import main


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_delimiter_detection_for_supported_formats(delimiter):
    text = delimiter.join(["id", "name", "price"]) + "\n"
    text += delimiter.join(["1", "Widget", "2.5"]) + "\n"
    assert main._detect_csv_delimiter(text) == delimiter


def test_csv_delimiter_detection_falls_back_to_consistent_header(monkeypatch):
    class FailedSniffer:
        def sniff(self, sample, delimiters):
            raise main.csv.Error("not enough data")

    monkeypatch.setattr(main.csv, "Sniffer", FailedSniffer)
    assert main._detect_csv_delimiter("product_id;product_name;price\n1;Widget;2\n") == ";"


class UploadCursor:
    def __init__(self):
        self.statements = []
        self.inserted = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if "GET_LOCK" in statement:
            self.row = (1,)
        elif "RELEASE_LOCK" in statement:
            self.row = (1,)
        else:
            self.row = None

    def fetchone(self):
        return getattr(self, "row", None)

    def executemany(self, statement, rows):
        self.inserted.extend(rows)

    def close(self):
        pass


class UploadConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def cursor(self, *args, **kwargs):
        return self.fake_cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_upload_skips_malformed_rows_and_preserves_empty_fields(monkeypatch):
    cursor = UploadCursor()
    monkeypatch.setattr(main, "get_connection", lambda: UploadConnection(cursor))
    monkeypatch.setattr(main, "_public_rate_limit", lambda *args: None)
    client = TestClient(main.app)
    payload = (
        "product_id;product_name;category;price;stock;added_on\n"
        "1;Widget;Tools;2.5;;2025-01-01\n"
        "2;Gadget;Home;4;3;2025-01-02\n"
        "3;Too;Few\n"
        "4;Too;Many;1;2;2025-01-04;extra\n"
        ";Empty ID;Home;1;2;2025-01-05\n"
    )
    response = client.post(
        "/api/upload-csv",
        files={"file": ("products.csv", io.BytesIO(payload.encode("utf-8")), "text/csv")},
        data={"table_name": "up_csv_delimiter_test"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert [column["name"] for column in result["columns"]] == [
        "product_id", "product_name", "category", "price", "stock", "added_on"
    ]
    assert result["row_count"] == 3
    assert result["skipped_rows"] == 2
    assert cursor.inserted[0][4] is None
    assert cursor.inserted[2][0] is None


def test_upload_empty_header_names_are_sanitized(monkeypatch):
    cursor = UploadCursor()
    monkeypatch.setattr(main, "get_connection", lambda: UploadConnection(cursor))
    monkeypatch.setattr(main, "_public_rate_limit", lambda *args: None)
    response = TestClient(main.app).post(
        "/api/upload-csv",
        files={"file": ("empty.csv", b",name\n1,Widget\n", "text/csv")},
        data={"table_name": "up_csv_empty_headers"},
    )
    assert response.status_code == 200, response.text
    assert [column["name"] for column in response.json()["columns"]] == ["col_1", "name"]
