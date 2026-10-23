# AI-Assisted SQL Query Analyzer

Explore MySQL execution plans as an interactive graph. The app includes a library of example plans, rule-based warnings, AI-style summaries and suggestions, and playback controls that step through each plan node.

## Architecture

- `main.py` exposes the FastAPI endpoints and serves the frontend.
- `core/parser.py` converts textual `EXPLAIN ANALYZE` output into a plan tree.
- `core/rules.py` finds scan, estimate, sort, temporary table, and timing issues.
- `core/steps.py` turns a plan tree into post-order animation steps.
- `core/service.py` loads captured or fallback plans and calculates summary statistics.
- `core/ai.py` returns concise suggestions from the plan warnings.
- `frontend/` renders the graph with Cytoscape and Dagre and provides query, warning, insight, and animation panels.
- `db.py` runs live, read-only `EXPLAIN ANALYZE` requests against MySQL.

The saved plan viewer works from the checked-in demo plans. Live analysis through `POST /api/analyze` requires a supported MySQL server.

## Setup

Use Python 3.10 or later. From the repository root, create an environment and install the requirements:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

To enable live query analysis, configure `.env` from `.env.example` with the read-only database credentials. MySQL 8.0.18 or later is required. To create the sample database, set `MYSQL_ROOT_PASSWORD` and run:

```powershell
$env:MYSQL_ROOT_PASSWORD = "your_mysql_root_password"
python setup_db.py
```

Start the application from the repository root:

```powershell
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. The default graph is the `08_full_pipeline` example. Select another query from the left sidebar; use the bottom controls to play, pause, step through, reset, or change playback speed.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Returns service status. |
| `GET` | `/api/queries` | Lists example query IDs, titles, and SQL. |
| `GET` | `/api/plan/{id}` | Returns a saved plan, warnings, animation steps, statistics, and plan source. |
| `POST` | `/api/ai/{id}` | Returns a summary and suggestions derived from the saved plan's warnings. |
| `POST` | `/api/analyze` | Runs live `EXPLAIN ANALYZE` for a validated SQL query. Requires MySQL configuration. |

`POST /api/analyze` accepts JSON such as:

```json
{"query":"SELECT * FROM orders WHERE customer_id = 7"}
```

## Tests

Run the full test suite from the repository root:

```powershell
pytest -v
```

Parser, rule, and animation tests use the checked-in plans. Database integration tests require the configured MySQL service; they skip when MySQL is unavailable. The security tests validate the read-only SQL query checks.
