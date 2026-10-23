import threading
import json
from pathlib import Path

import mysql.connector
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, constr

from core.ai import get_ai_explanation
from core.parser import parse_explain_analyze
from core.rules import analyze_rules, annotate_tree
from core.service import calculate_stats, load_plan_text
from core.steps import build_animation_steps
from db import UnsupportedServerError, run_explain_analyze
from security import UnsafeQueryError

app = FastAPI()
_slots = threading.BoundedSemaphore(5)
_PROJECT_ROOT = Path(__file__).resolve().parent
_QUERIES_FILE = _PROJECT_ROOT / "demo" / "queries.json"


def _read_queries():
    with _QUERIES_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@app.get("/api/health")
def health():
    return {"status": "ok"}


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
    return get_ai_explanation(query["sql"], warnings)

class AnalyzeRequest(BaseModel):
    query: constr(max_length=5000)

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
