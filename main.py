import threading
from pathlib import Path

import mysql.connector
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, constr

from db import UnsupportedServerError, run_explain_analyze
from security import UnsafeQueryError

app = FastAPI()
_slots = threading.BoundedSemaphore(5)

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

_frontend = Path(__file__).resolve().parent / "frontend"
app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
