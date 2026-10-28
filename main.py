import csv
import io
import json
import logging
import math
import os
import re
import threading
import asyncio
import ipaddress
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from math import ceil

_SHELL_DB_ENV_AT_IMPORT = {
    name.upper(): value
    for name, value in os.environ.items()
    if name.upper().startswith(("DB_", "MYSQL_"))
}

import mysql.connector
from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, constr

from core.ai import get_ai_explanation, get_rule_based_explanation
from core.parser import parse_explain_analyze
from core.rules import analyze_rules, annotate_dead_end_nodes, annotate_tree
from core.service import calculate_stats, load_plan_text
from core.steps import build_animation_steps
from db import (
    DatabaseUploadConfigurationError,
    IndexSimulationConflictError,
    IndexSimulationValidationError,
    UnsupportedServerError,
    UploadColumnNotFoundError,
    UploadTableNotFoundError,
    UPLOAD_SCHEMA,
    build_row_flow,
    cleanup_expired_uploads,
    ensure_upload_schema,
    execute_sql_file,
    get_connection,
    run_explain_analyze,
    run_query_with_data,
    safe_database_error_message,
    simulate_index as run_index_simulation,
)
from public_safety import InMemoryRateLimiter
from security import UnsafeQueryError

load_dotenv(Path(__file__).resolve().with_name(".env"))


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


PUBLIC_MODE = _env_flag("PUBLIC_MODE")
_LOGGER = logging.getLogger(__name__)
_PUBLIC_RATE_LIMITER = InMemoryRateLimiter()
_PUBLIC_LIMITS = {
    "query": (30, 60, "queries per minute"),
    "ai": (10, 60, "AI calls per minute"),
    "upload": (5, 60 * 60, "uploads per hour"),
}
_UPLOAD_CAP_LOCK = "sql_analyzer_upload_table_cap"
_PUBLIC_UPLOAD_TABLE_LIMIT = 5


def _log_shell_db_overrides() -> None:
    env_values = dotenv_values(_PROJECT_ROOT / ".env", interpolate=False)
    env_values = {str(name).upper(): value for name, value in env_values.items() if name}
    overridden = [
        name
        for name, shell_value in _SHELL_DB_ENV_AT_IMPORT.items()
        if name in env_values and str(env_values[name]) != shell_value
    ]
    if overridden:
        _LOGGER.warning(
            "Shell environment overrides .env settings for: %s",
            ", ".join(sorted(overridden)),
        )


async def _public_upload_cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_expired_uploads)
        except mysql.connector.Error as exc:
            _LOGGER.warning("Public upload cleanup failed (database error code %s).", getattr(exc, "errno", "unknown"))
        except Exception as exc:
            _LOGGER.warning("Public upload cleanup failed (%s).", type(exc).__name__)
        await asyncio.sleep(5 * 60)


@asynccontextmanager
async def _lifespan(_app):
    _log_shell_db_overrides()
    try:
        ensure_upload_schema()
    except mysql.connector.Error as exc:
        _LOGGER.warning(
            "Upload schema initialization failed (%s): %s",
            type(exc).__name__,
            safe_database_error_message(exc),
        )
    except Exception as exc:
        _LOGGER.warning(
            "Upload schema initialization failed (%s): %s",
            type(exc).__name__,
            safe_database_error_message(exc),
        )

    cleanup_task = asyncio.create_task(_public_upload_cleanup_loop()) if PUBLIC_MODE else None
    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass


app = FastAPI(lifespan=_lifespan)
_slots = threading.BoundedSemaphore(5)
_PROJECT_ROOT = Path(__file__).resolve().parent
_QUERIES_FILE = _PROJECT_ROOT / "demo" / "queries.json"
_MAX_SQL_FILE_BYTES = 10_000_000
_MAX_CSV_BYTES = 5 * 1024 * 1024
_MAX_CSV_ROWS = 50_000
_MAX_CSV_COLUMNS = 50
_CSV_RESERVED_WORDS = {
    "accessible", "add", "all", "alter", "analyze", "and", "as", "asc", "asensitive",
    "before", "between", "bigint", "binary", "blob", "both", "by", "call", "cascade",
    "case", "change", "char", "character", "check", "collate", "column", "columns",
    "condition", "constraint", "continue", "convert", "create", "cross", "cube", "cume_dist",
    "current_date", "current_time", "current_timestamp", "current_user", "cursor", "database",
    "databases", "day_hour", "day_microsecond", "day_minute", "day_second", "dec", "decimal",
    "declare", "default", "delayed", "delete", "dense_rank", "desc", "describe", "deterministic",
    "distinct", "distinctrow", "div", "double", "drop", "dual", "each", "else", "elseif",
    "empty", "enclosed", "escaped", "except", "exists", "exit", "explain", "false", "fetch",
    "first_value", "float", "for", "force", "foreign", "from", "fulltext", "function", "generated",
    "get", "grant", "group", "groups", "having", "high_priority", "hour_microsecond", "hour_minute",
    "hour_second", "if", "ignore", "in", "index", "infile", "inner", "inout", "insensitive",
    "insert", "int", "integer", "intersect", "interval", "into", "io_after_gtids", "io_before_gtids",
    "is", "iterate", "join", "json_table", "key", "keys", "kill", "lag", "last_value", "lateral",
    "lead", "leading", "leave", "left", "like", "limit", "linear", "lines", "load", "localtime",
    "localtimestamp", "lock", "long", "longblob", "longtext", "loop", "low_priority", "master_bind",
    "master_ssl_verify_server_cert", "match", "maxvalue", "mediumblob", "mediumint", "mediumtext",
    "middleint", "minute_microsecond", "minute_second", "mod", "modifies", "natural", "not", "no_write_to_binlog",
    "nth_value", "ntile", "null", "numeric", "of", "on", "optimize", "option", "optionally", "or",
    "order", "out", "outer", "outfile", "over", "partition", "percent_rank", "precision", "primary",
    "procedure", "purge", "range", "rank", "read", "reads", "read_write", "real", "recursive", "references",
    "regexp", "release", "rename", "repeat", "replace", "require", "resignal", "restrict", "return",
    "revoke", "right", "rlike", "row", "rows", "row_number", "schema", "schemas", "second_microsecond",
    "select", "sensitive", "separator", "set", "show", "signal", "smallint", "spatial", "specific",
    "sql", "sqlexception", "sqlstate", "sqlwarning", "sql_big_result", "sql_calc_found_rows", "sql_small_result",
    "ssl", "starting", "stored", "straight_join", "system", "table", "terminated", "then", "tinyblob",
    "tinyint", "tinytext", "to", "trailing", "trigger", "true", "undo", "union", "unique", "unlock",
    "unsigned", "update", "usage", "use", "using", "utc_date", "utc_time", "utc_timestamp", "values",
    "varbinary", "varchar", "varying", "virtual", "when", "where", "while", "window", "with", "write",
    "xor", "year_month", "zerofill",
}


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        candidate = forwarded_for.split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    remote = request.client.host if request.client else "unknown"
    try:
        return str(ipaddress.ip_address(remote))
    except ValueError:
        return remote or "unknown"


def _public_rate_limit(request: Request, category: str) -> JSONResponse | None:
    if not PUBLIC_MODE:
        return None
    limit, window_seconds, label = _PUBLIC_LIMITS[category]
    retry_after = _PUBLIC_RATE_LIMITER.consume(
        _client_ip(request), category, limit, window_seconds
    )
    if retry_after is None:
        return None
    wait_seconds = max(1, ceil(retry_after))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(wait_seconds)},
        content={"error": f"Rate limit exceeded: maximum {limit} {label}. Try again in {wait_seconds} seconds."},
    )


@contextmanager
def _upload_table_slot(cursor):
    if not PUBLIC_MODE:
        yield
        return

    cursor.execute("SELECT GET_LOCK(%s, 10)", (_UPLOAD_CAP_LOCK,))
    lock_row = cursor.fetchone()
    acquired = bool(lock_row and lock_row[0] == 1)
    if not acquired:
        raise HTTPException(status_code=429, detail="Upload capacity is busy. Retry shortly.")
    try:
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
            (UPLOAD_SCHEMA,),
        )
        existing_tables = [
            str(row[0])
            for row in cursor.fetchall()
            if re.fullmatch(r"up_[a-z0-9_]{1,61}", str(row[0]))
            and str(row[0]) != "up_temp_clone"
        ]
        if len(existing_tables) >= _PUBLIC_UPLOAD_TABLE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="At most 5 uploaded tables can exist at once in public mode. Delete a table or wait for cleanup.",
            )
        yield
    finally:
        try:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (_UPLOAD_CAP_LOCK,))
            cursor.fetchone()
        except mysql.connector.Error:
            pass


def _sanitize_csv_header(value: str, index: int, used: set[str]) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not name:
        name = f"col_{index + 1}"
    if name[0].isdigit():
        name = f"col_{name}"
    if name in _CSV_RESERVED_WORDS:
        name += "_"
    base = name[:64]
    name = base
    suffix = 2
    while name in used:
        suffix_text = f"_{suffix}"
        name = f"{base[:64 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(name)
    return name


def _csv_date(value: str) -> bool:
    value = value.strip()
    for parser in (date.fromisoformat,):
        try:
            parser(value)
            return True
        except ValueError:
            pass
    for pattern in ("%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            datetime.strptime(value, pattern)
            return True
        except ValueError:
            pass
    return False


def _infer_csv_type(values: list[str | None]) -> str:
    nonempty = [value for value in values if value is not None and value.strip()]
    if not nonempty:
        return "VARCHAR(32)"
    stripped = [value.strip() for value in nonempty]
    if all(re.fullmatch(r"[+-]?\d+", value) for value in stripped):
        integers = [int(value) for value in stripped]
        if all(-(2**31) <= value <= (2**31 - 1) for value in integers):
            return "INT"
        return _infer_csv_string_type(nonempty)
    if all(_csv_date(value) for value in stripped):
        return "DATE"
    try:
        if all(math.isfinite(float(value)) for value in stripped):
            return "DOUBLE"
    except ValueError:
        pass
    return _infer_csv_string_type(nonempty)


def _infer_csv_string_type(values: list[str]) -> str:
    longest = max((len(value) for value in values), default=0)
    if longest > 255:
        return "TEXT"
    return f"VARCHAR({max(32, longest)})"


def _upload_table_identifier(table: str) -> str:
    if not re.fullmatch(r"up_[a-z0-9_]{1,61}", table) or table == "up_temp_clone":
        raise HTTPException(status_code=400, detail="Invalid upload table name.")
    return f"`analyzer_uploads`.`{table}`"


def _csv_upload_name(filename: str, table_name: str | None = None) -> str:
    stem = Path(table_name or filename).stem.casefold()
    sanitized = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    if not sanitized:
        sanitized = "upload"
    return f"up_{sanitized[:61]}"


def _read_queries():
    with _QUERIES_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@app.get("/api/health")
def health():
    conn = None
    try:
        conn = get_connection()
        with closing(conn.cursor()) as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        _LOGGER.warning(
            "Database health check failed (%s): %s",
            type(exc).__name__,
            safe_database_error_message(exc),
        )
        if getattr(exc, "errno", None) == 1045:
            return {"database": "auth failed"}
        return {"db": "down"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                _LOGGER.warning(
                    "Database health check connection close failed (%s): %s",
                    type(exc).__name__,
                    safe_database_error_message(exc),
                )
    return {"status": "ok", "database": "connected", "db": "up"}


@app.post("/api/upload-sql")
async def upload_sql_file(http_request: Request, file: UploadFile = File(...)):
    limited = _public_rate_limit(http_request, "upload")
    if limited:
        return limited
    if PUBLIC_MODE:
        raise HTTPException(status_code=403, detail="SQL script uploads are disabled in public mode.")

    filename = file.filename or ""
    if Path(filename).suffix.casefold() not in {".sql", ".txt"}:
        raise HTTPException(status_code=400, detail="Choose a .sql or .txt file.")

    content = await file.read(_MAX_SQL_FILE_BYTES + 1)
    if len(content) > _MAX_SQL_FILE_BYTES:
        raise HTTPException(status_code=413, detail="SQL files must be 10 MB or smaller.")
    try:
        sql_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="The SQL file must use UTF-8 encoding.") from exc

    try:
        execute_sql_file(sql_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatabaseUploadConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except mysql.connector.Error as exc:
        raise HTTPException(status_code=400, detail=f"SQL upload failed: {exc.msg}") from exc

    return {"ok": True, "message": "Database updated successfully"}


@app.post("/api/upload-csv")
async def upload_csv_file(http_request: Request, file: UploadFile = File(...), table_name: str = Form("")):
    limited = _public_rate_limit(http_request, "upload")
    if limited:
        return limited

    filename = file.filename or ""
    if Path(filename).suffix.casefold() != ".csv":
        raise HTTPException(status_code=400, detail="Choose a .csv file.")

    content = await file.read(_MAX_CSV_BYTES + 1)
    if len(content) > _MAX_CSV_BYTES:
        raise HTTPException(status_code=400, detail="CSV files must be 5 MB or smaller.")
    try:
        csv_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="The CSV file must use UTF-8 encoding.") from exc
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="The CSV file must include a header row.")

    sample = csv_text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(csv_text, newline=""), dialect=dialect, strict=True)
    try:
        headers = next(reader, None)
        if not headers:
            raise HTTPException(status_code=400, detail="The CSV file must include a header row.")
        if len(headers) > _MAX_CSV_COLUMNS:
            raise HTTPException(status_code=400, detail="CSV files may contain at most 50 columns.")
        used_headers: set[str] = set()
        columns = [
            {
                "name": _sanitize_csv_header(header, index, used_headers),
                "original_name": header,
                "type": "",
            }
            for index, header in enumerate(headers)
        ]
        data_rows: list[list[str | None]] = []
        seen_rows = 0
        skipped_rows = 0
        for row in reader:
            seen_rows += 1
            if seen_rows > _MAX_CSV_ROWS:
                raise HTTPException(status_code=400, detail="CSV files may contain at most 50,000 rows.")
            if len(row) > _MAX_CSV_COLUMNS:
                raise HTTPException(status_code=400, detail="CSV files may contain at most 50 columns.")
            if len(row) != len(headers):
                skipped_rows += 1
                continue
            data_rows.append([None if not value.strip() else value for value in row])
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail="The CSV file contains malformed rows.") from exc

    for index, column in enumerate(columns):
        column["type"] = _infer_csv_type([row[index] for row in data_rows])

    table = _csv_upload_name(filename, table_name.strip() or None)
    qualified_table = _upload_table_identifier(table)
    definitions = ", ".join(
        f"`{column['name']}` {column['type']}" for column in columns
    )
    create_sql = f"CREATE TABLE {qualified_table} ({definitions})"
    insert_sql = (
        f"INSERT INTO {qualified_table} ("
        + ", ".join(f"`{column['name']}`" for column in columns)
        + ") VALUES ("
        + ", ".join(["%s"] * len(columns))
        + ")"
    )

    conn = get_connection()
    table_created = False
    try:
        with closing(conn.cursor()) as cursor:
            with _upload_table_slot(cursor):
                try:
                    cursor.execute(create_sql)
                    table_created = True
                    for offset in range(0, len(data_rows), 1000):
                        cursor.executemany(insert_sql, data_rows[offset : offset + 1000])
                    conn.commit()
                except mysql.connector.Error as exc:
                    conn.rollback()
                    if table_created:
                        try:
                            cursor.execute(f"DROP TABLE {qualified_table}")
                            conn.commit()
                        except mysql.connector.Error:
                            conn.rollback()
                    if getattr(exc, "errno", None) == 1050:
                        raise HTTPException(status_code=400, detail="An upload with this table name already exists.") from exc
                    raise HTTPException(status_code=500, detail="Could not create the upload table.") from exc
    finally:
        conn.close()

    return {
        "ok": True,
        "table": table,
        "qualified_name": f"analyzer_uploads.{table}",
        "row_count": len(data_rows),
        "skipped_rows": skipped_rows,
        "columns": columns,
        "sample_query": f"SELECT * FROM {qualified_table} LIMIT 10",
    }


@app.get("/api/uploads")
def list_csv_uploads():
    conn = get_connection()
    uploads = []
    try:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = 'analyzer_uploads' ORDER BY TABLE_NAME"
            )
            tables = [
                str(row[0])
                for row in cursor.fetchall()
                if str(row[0]).startswith("up_") and str(row[0]) != "up_temp_clone"
            ]
            for table in tables:
                qualified_table = _upload_table_identifier(table)
                cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = 'analyzer_uploads' AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (table,),
                )
                columns = [{"name": str(row[0]), "type": str(row[1]).upper()} for row in cursor.fetchall()]
                cursor.execute(f"SELECT COUNT(*) FROM {qualified_table}")
                row = cursor.fetchone()
                uploads.append({
                    "table": table,
                    "qualified_name": f"analyzer_uploads.{table}",
                    "row_count": int(row[0]) if row else 0,
                    "columns": columns,
                })
    except mysql.connector.Error as exc:
        raise HTTPException(status_code=503, detail="Could not list uploaded tables.") from exc
    finally:
        conn.close()
    return {"uploads": uploads}


@app.delete("/api/uploads/{table}")
def delete_csv_upload(table: str):
    qualified_table = _upload_table_identifier(table)
    conn = get_connection()
    try:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = 'analyzer_uploads' AND TABLE_NAME = %s LIMIT 1",
                (table,),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Upload table not found.")
            cursor.execute(f"DROP TABLE {qualified_table}")
            conn.commit()
    except HTTPException:
        raise
    except mysql.connector.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Could not delete the uploaded table.") from exc
    finally:
        conn.close()
    return {"ok": True, "table": table}


@app.get("/api/queries")
def get_queries():
    return _read_queries()


@app.get("/api/plan/{id}")
def get_plan(id: str):
    queries = _read_queries()
    query_obj = next((query for query in queries if query.get("id") == id), None)
    if query_obj is None:
        raise HTTPException(status_code=404, detail="Query not found")

    text, source = load_plan_text(id)
    tree = parse_explain_analyze(text)
    annotate_dead_end_nodes(tree)
    warnings = analyze_rules(tree)
    annotate_tree(tree, warnings)
    steps = build_animation_steps(tree)
    stats = calculate_stats(tree)
    return {
        "query": query_obj,
        "tree": tree,
        "animation_steps": steps,
        "warnings": warnings,
        "stats": stats,
        "source": source,
    }


@app.post("/api/ai/{id}")
def get_ai_insights(id: str, http_request: Request):
    limited = _public_rate_limit(http_request, "ai")
    if limited:
        return limited
    queries = _read_queries()
    query = next((item for item in queries if item.get("id") == id), None)
    if query is None:
        raise HTTPException(status_code=404, detail="Query not found")

    text, _source = load_plan_text(id)
    tree = parse_explain_analyze(text)
    warnings = analyze_rules(tree)
    return get_rule_based_explanation(query["sql"], warnings)

class AnalyzeRequest(BaseModel):
    query: constr(max_length=5000)


class LiveQueryRequest(BaseModel):
    query: constr(max_length=5000)


class IndexSimulationRequest(BaseModel):
    query: constr(max_length=5000)
    table_name: constr(min_length=1, max_length=64)
    column_name: constr(min_length=1, max_length=64)


class AIRequest(BaseModel):
    query: constr(max_length=5000)
    warnings: list[dict] = Field(default_factory=list)
    error: dict | None = None
    tree: dict | None = None
    schema_info: dict | None = None


@app.post("/api/run-live")
def run_live(request: LiveQueryRequest, http_request: Request):
    limited = _public_rate_limit(http_request, "query")
    if limited:
        return limited
    try:
        result = run_query_with_data(request.query)
    except UnsafeQueryError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except (mysql.connector.InterfaceError, mysql.connector.OperationalError) as exc:
        if getattr(exc, "errno", None) in (2002, 2003, 2005, 1045, 1049, 2006, 2013, 2055):
            return JSONResponse(status_code=503, content={"error": "db_unavailable"})
        return {
            "ok": False,
            "error": {"code": str(getattr(exc, "errno", "MYSQL_ERROR")), "message": str(exc)},
            "query": request.query,
        }
    except UnsupportedServerError:
        return JSONResponse(status_code=503, content={"error": "db_unavailable"})
    except mysql.connector.Error as exc:
        return {
            "ok": False,
            "error": {"code": str(getattr(exc, "errno", "MYSQL_ERROR")), "message": str(exc)},
            "query": request.query,
        }
    except OSError:
        return JSONResponse(status_code=503, content={"error": "db_unavailable"})

    tree = parse_explain_analyze(result["plan_text"])
    tree["_flow_query"] = request.query
    flow_conn = get_connection()
    try:
        row_flow = build_row_flow(tree, flow_conn, result["source_samples"])
    finally:
        flow_conn.close()
        tree.pop("_flow_query", None)

    row_records = {}
    for flow_step in row_flow:
        row_records.update(flow_step.pop("row_data", {}))
    annotate_dead_end_nodes(tree)
    warnings = analyze_rules(tree)
    annotate_tree(tree, warnings)
    steps = build_animation_steps(tree)
    stats = calculate_stats(tree)
    return {
        "query": request.query,
        "tree": tree,
        "steps": steps,
        "row_flow": row_flow,
        "row_records": row_records,
        "warnings": warnings,
        "stats": stats,
        "rows": result["rows"],
        "columns": result["columns"],
        "source_samples": result["source_samples"],
        "schema_info": result.get("schema_info"),
    }


@app.post("/api/simulate-index")
def simulate_index_endpoint(request: IndexSimulationRequest, http_request: Request):
    limited = _public_rate_limit(http_request, "query")
    if limited:
        return limited
    if not _slots.acquire(blocking=False):
        return JSONResponse(status_code=429, content={"ok": False, "error": "Server busy"})
    try:
        result = run_index_simulation(request.table_name, request.column_name, request.query)
        return {"ok": True, **result}
    except (UnsafeQueryError, IndexSimulationValidationError) as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except (UploadTableNotFoundError, UploadColumnNotFoundError) as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": str(exc)})
    except IndexSimulationConflictError as exc:
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
    except TimeoutError:
        return JSONResponse(status_code=408, content={"ok": False, "error": "Query exceeded 5s limit"})
    except UnsupportedServerError:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Database unsupported"})
    except mysql.connector.Error as exc:
        if getattr(exc, "errno", None) in (2002, 2003, 2005, 1045, 1049, 2006, 2013, 2055, 1142):
            return JSONResponse(status_code=503, content={"ok": False, "error": "Index simulation is unavailable for this database account."})
        return JSONResponse(status_code=500, content={"ok": False, "error": "Index simulation failed."})
    except OSError:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Database unavailable"})
    finally:
        _slots.release()


@app.post("/api/ai")
def get_live_ai_insights(request: AIRequest, http_request: Request):
    limited = _public_rate_limit(http_request, "ai")
    if limited:
        return limited
    try:
        explanation = get_ai_explanation(
            request.query,
            request.warnings,
            tree_summary=request.tree,
            error=request.error,
            schema_info=request.schema_info,
        )
        if request.error and not (isinstance(explanation.get("fix"), str) and explanation["fix"].strip()):
            explanation["fix"] = get_rule_based_explanation(
                request.query,
                request.warnings,
                tree_summary=request.tree,
                error=request.error,
            )["fix"]
        is_live = explanation.get("ai_status") == "success"
        return {
            **explanation,
            "source": "gemini" if is_live else "fallback",
            "note": "" if is_live else explanation.get("reason", "Gemini unavailable; using rule-based insights."),
        }
    except Exception:
        explanation = get_rule_based_explanation(
            request.query,
            request.warnings,
            tree_summary=request.tree,
            error=request.error,
        )
        return {
            **explanation,
            "ai_status": "fallback",
            "source": "fallback",
            "note": "Gemini unavailable; using rule-based insights.",
            "reason": "Gemini request failed",
        }

@app.post("/api/analyze")
def analyze(request: AnalyzeRequest, http_request: Request):
    limited = _public_rate_limit(http_request, "query")
    if limited:
        return limited
    if not _slots.acquire(blocking=False):
        return JSONResponse(status_code=429, content={"success": False, "error": "Server busy"})
    try:
        return {"success": True, **run_explain_analyze(request.query)}
    except UnsafeQueryError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": f"Unsafe query blocked: {exc}"})
    except TimeoutError:
        return JSONResponse(status_code=408, content={"success": False, "error": "Query exceeded 5s limit"})
    except UnsupportedServerError:
        return JSONResponse(status_code=503, content={"success": False, "error": "Database unsupported"})
    except mysql.connector.Error as exc:
        if exc.errno in (2002, 2003, 1045):
            return JSONResponse(status_code=503, content={"success": False, "error": "Database unavailable"})
        if exc.errno in (1064, 1146, 1054, 1142):
            return JSONResponse(status_code=400, content={"success": False, "error": f"MySQL Error: {exc.msg}"})
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})
    except Exception:
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})
    finally:
        _slots.release()

@app.get("/", include_in_schema=False)
def welcome_page():
    return FileResponse(_PROJECT_ROOT / "frontend" / "welcome.html")


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT") or "8000"),
    )
