import threading
import json
from pathlib import Path

import mysql.connector
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, constr

from core.ai import get_ai_explanation, get_rule_based_explanation
from core.parser import parse_explain_analyze
from core.rules import analyze_rules, annotate_tree
from core.service import calculate_stats, load_plan_text
from core.steps import build_animation_steps
from db import (
    DatabaseUploadConfigurationError,
    UnsupportedServerError,
    build_row_flow,
    execute_sql_file,
    get_connection,
    run_explain_analyze,
    run_query_with_data,
)
from security import UnsafeQueryError

app = FastAPI()
_slots = threading.BoundedSemaphore(5)
_PROJECT_ROOT = Path(__file__).resolve().parent
_QUERIES_FILE = _PROJECT_ROOT / "demo" / "queries.json"
_MAX_SQL_FILE_BYTES = 10_000_000


def _read_queries():
    with _QUERIES_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/upload-sql")
async def upload_sql_file(file: UploadFile = File(...)):
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
def get_ai_insights(id: str):
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


class AIRequest(BaseModel):
    query: constr(max_length=5000)
    warnings: list[dict] = Field(default_factory=list)
    error: dict | None = None
    tree: dict | None = None


@app.post("/api/run-live")
def run_live(request: LiveQueryRequest):
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
        row_flow = build_row_flow(tree, flow_conn)
    finally:
        flow_conn.close()
        tree.pop("_flow_query", None)

    row_records = {}
    for flow_step in row_flow:
        row_records.update(flow_step.pop("row_data", {}))
    warnings = analyze_rules(tree)

    def mark_dead_ends(node):
        if node.get("actual_rows") == 0:
            node["dead_end"] = True
            warnings.append({
                "code": "DEAD_END",
                "type": "DEAD_END",
                "severity": "info",
                "node_id": node["id"],
                "title": "No rows produced",
                "message": f"{node.get('operator') or 'This operator'} produced no rows.",
                "suggestion": None,
            })
        for child in node.get("children", []):
            mark_dead_ends(child)

    mark_dead_ends(tree)
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
    }


@app.post("/api/ai")
def get_live_ai_insights(request: AIRequest):
    try:
        explanation = get_ai_explanation(
            request.query,
            request.warnings,
            tree_summary=request.tree,
            error=request.error,
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
def analyze(request: AnalyzeRequest):
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

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
