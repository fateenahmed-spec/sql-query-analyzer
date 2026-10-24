from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.requests import Request
import mysql.connector

import db
import main
from public_safety import InMemoryRateLimiter
from security import UnsafeQueryError


class FakeCursor:
    def __init__(self, table_rows=()):
        self.table_rows = list(table_rows)
        self.rows = []
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        normalized = statement.casefold()
        if "get_lock" in normalized:
            self.rows = [(1,)]
        elif "information_schema.tables" in normalized:
            self.rows = list(self.table_rows)
        elif "release_lock" in normalized:
            self.rows = [(1,)]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.closed = False
        self.committed = False

    def cursor(self, *args, **kwargs):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_rate_limiter_uses_a_rolling_window():
    now = [100.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])

    assert limiter.consume("198.51.100.8", "query", 2, 60) is None
    assert limiter.consume("198.51.100.8", "query", 2, 60) is None
    assert limiter.consume("198.51.100.8", "query", 2, 60) == 60
    now[0] += 60
    assert limiter.consume("198.51.100.8", "query", 2, 60) is None


def test_database_connection_reads_port_and_enables_verified_ssl(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("DB_HOST", "mysql.example.test")
    monkeypatch.setenv("DB_PORT", "24876")
    monkeypatch.setenv("DB_USER", "deployment-user")
    monkeypatch.setenv("DB_PASS", "deployment-placeholder")
    monkeypatch.setenv("DB_NAME", "deployment-db")
    monkeypatch.setenv("DB_SSL", "true")
    ca_path = tmp_path / "cloud-ca.pem"
    ca_path.write_text("placeholder certificate", encoding="utf-8")
    monkeypatch.setenv("DB_SSL_CA", str(ca_path))
    monkeypatch.setattr(db, "_SSL_CA_RESOLVED", db._SSL_CA_UNRESOLVED)
    connect = mocker.patch("db.mysql.connector.connect")

    db.get_connection()

    options = connect.call_args.kwargs
    assert options["host"] == "mysql.example.test"
    assert options["port"] == 24876
    assert options["database"] == "deployment-db"
    assert options["ssl_disabled"] is False
    assert options["ssl_ca"] == str(ca_path.resolve())
    assert options["ssl_verify_cert"] is True
    assert options["ssl_verify_identity"] is True


def test_local_database_connection_always_uses_unverified_tls(monkeypatch, mocker):
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_SSL", "false")
    monkeypatch.delenv("DB_SSL_CA", raising=False)
    monkeypatch.setattr(db, "_SSL_CA_RESOLVED", db._SSL_CA_UNRESOLVED)
    connect = mocker.patch("db.mysql.connector.connect")

    db.get_connection()

    options = connect.call_args.kwargs
    assert options["host"] == "127.0.0.1"
    assert options["ssl_disabled"] is False
    assert options["ssl_verify_cert"] is False
    assert options["ssl_verify_identity"] is False
    assert options["client_flags"] == [db.ClientFlag.SSL]


def test_ensure_upload_schema_connects_without_selecting_a_database(mocker):
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    connect = mocker.patch("db.get_connection", return_value=conn)

    db.ensure_upload_schema()

    connect.assert_called_once_with(include_database=False)
    assert cursor.statements[0][0] == "CREATE DATABASE IF NOT EXISTS `analyzer_uploads`"
    assert conn.committed is True
    assert conn.closed is True


def test_client_ip_uses_the_first_forwarded_address():
    request = Request({
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.14, 10.0.0.3")],
        "client": ("10.0.0.2", 1234),
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
    })

    assert main._client_ip(request) == "203.0.113.14"


def test_health_returns_200_with_auth_failure_status(monkeypatch):
    monkeypatch.setattr(main, "ensure_upload_schema", lambda: None)
    monkeypatch.setattr(main, "cleanup_expired_uploads", lambda: [])

    def fail_auth():
        raise mysql.connector.Error("Access denied", errno=1045)

    monkeypatch.setattr(main, "get_connection", fail_auth)
    with TestClient(main.app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"database": "auth failed"}


def test_cleanup_drops_only_old_valid_upload_tables(monkeypatch):
    cursor = FakeCursor([
        ("up_expired",),
        ("ordinary_table",),
        ("up_bad-name",),
        ("up_temp_clone",),
    ])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(db, "get_connection", lambda **_kwargs: conn)

    dropped = db.cleanup_expired_uploads()

    assert dropped == ["up_expired"]
    drop_statements = [statement for statement, _ in cursor.statements if statement.startswith("DROP TABLE")]
    assert drop_statements == ["DROP TABLE IF EXISTS `analyzer_uploads`.`up_expired`"]
    assert "CREATE_TIME <= NOW() - INTERVAL 60 MINUTE" in cursor.statements[0][0]
    assert conn.committed is True
    assert conn.closed is True


def test_sixth_uploaded_table_is_rejected_in_public_mode(monkeypatch):
    cursor = FakeCursor([(f"up_existing_{number}",) for number in range(1, 6)])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(main, "get_connection", lambda: conn)
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    monkeypatch.setattr(main, "_PUBLIC_RATE_LIMITER", InMemoryRateLimiter())
    monkeypatch.setattr(main, "ensure_upload_schema", lambda: None)
    monkeypatch.setattr(main, "cleanup_expired_uploads", lambda: [])

    with TestClient(main.app) as client:
        response = client.post(
            "/api/upload-csv",
            files={"file": ("sixth.csv", b"city\nPune\n", "text/csv")},
        )

    assert response.status_code == 429
    assert "At most 5 uploaded tables" in response.json()["detail"]
    assert conn.closed is True


def test_public_query_ai_and_upload_limits_return_429(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    monkeypatch.setattr(main, "_PUBLIC_RATE_LIMITER", InMemoryRateLimiter())
    monkeypatch.setattr(main, "ensure_upload_schema", lambda: None)
    monkeypatch.setattr(main, "cleanup_expired_uploads", lambda: [])

    def reject_query(_query):
        raise UnsafeQueryError("test query rejected")

    monkeypatch.setattr(main, "run_query_with_data", reject_query)
    monkeypatch.setattr(
        main,
        "get_ai_explanation",
        lambda *_args, **_kwargs: {
            "summary": "Fallback explanation",
            "suggestions": [],
            "fix": None,
            "ai_status": "fallback",
            "reason": "API key missing",
        },
    )

    with TestClient(main.app) as client:
        headers = {"X-Forwarded-For": "198.51.100.28, 10.0.0.2"}
        for _ in range(30):
            response = client.post("/api/run-live", json={"query": "SELECT 1"}, headers=headers)
            assert response.status_code == 400
        response = client.post("/api/run-live", json={"query": "SELECT 1"}, headers=headers)
        assert response.status_code == 429
        assert "30 queries per minute" in response.json()["error"]
        assert response.headers["Retry-After"]

        for _ in range(10):
            response = client.post("/api/ai", json={"query": "SELECT 1"}, headers=headers)
            assert response.status_code == 200
            assert response.json()["source"] == "fallback"
        response = client.post("/api/ai", json={"query": "SELECT 1"}, headers=headers)
        assert response.status_code == 429
        assert "10 AI calls per minute" in response.json()["error"]

        upload = {"file": ("plan.sql", b"SELECT 1", "text/plain")}
        for _ in range(5):
            response = client.post("/api/upload-sql", files=upload, headers=headers)
            assert response.status_code == 403
        response = client.post("/api/upload-sql", files=upload, headers=headers)
        assert response.status_code == 429
        assert "5 uploads per hour" in response.json()["error"]
