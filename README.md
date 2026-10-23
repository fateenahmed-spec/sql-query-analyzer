# AI-Assisted SQL Query Analyzer

Explore MySQL execution plans as a four-step workflow: submit a read-only query, inspect the plan graph, follow sampled data and real operator statistics, then review AI suggestions. The saved demo plans work offline without MySQL or a Gemini key.

Get a free key at aistudio.google.com. Do NOT enable billing.

## Architecture

- `main.py` exposes the FastAPI API and serves the static frontend.
- `security.py` validates SQL as a read-only `SELECT` or `WITH` statement.
- `db.py` runs MySQL `EXPLAIN ANALYZE`, fetches up to 20 result rows, and samples up to 10 rows from each referenced base table.
- `core/parser.py` converts MySQL plan text into a tree; `core/rules.py` annotates plan warnings.
- `core/steps.py` builds post-order animation steps; `core/service.py` loads demo plans and calculates statistics.
- `core/ai.py` requests structured Gemini explanations, caches successful responses, enforces a six-second live-call interval, and supplies offline rule-based fallback output.
- `frontend/` contains the vanilla HTML, CSS, and JavaScript wizard with a Cytoscape/Dagre plan graph.

## Setup

Use Python 3.10 or later. From the repository root, create and activate a virtual environment, then install the free dependencies:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env`. Set `DB_HOST`, `DB_USER`, `DB_PASS`, and `DB_NAME` to a MySQL 8.0.18+ database and a read-only account. For a local sample database, configure `MYSQL_ROOT_PASSWORD` and run:

```powershell
$env:MYSQL_ROOT_PASSWORD = "your_mysql_root_password"
python setup_db.py
```

Set `GEMINI_API_KEY` in `.env` to enable Gemini insights. The default model is `gemini-2.5-flash-lite`. AI responses are cached by query, warnings, and error; the service permits at most one uncached Gemini request every six seconds. If Gemini is unavailable or rate-limited, the app returns rule-based insights. Demo-plan insights use the offline rule-based path.

Start the server from the repository root:

```powershell
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Returns service status. |
| `GET` | `/api/queries` | Lists saved demo query IDs, titles, and SQL. |
| `GET` | `/api/plan/{id}` | Loads a saved plan with warnings, animation steps, statistics, and its captured/fallback source. |
| `POST` | `/api/run-live` | Validates and executes a read-only query; returns its plan, operator warnings, up to 20 real result rows, and up to 10 sample rows per referenced table. Requires MySQL. |
| `POST` | `/api/ai` | Returns a structured Gemini explanation, or rule-based fallback output when Gemini is unavailable. |
| `POST` | `/api/ai/{id}` | Returns offline rule-based insights for a saved demo plan. |
| `POST` | `/api/analyze` | Legacy endpoint for a live `EXPLAIN ANALYZE` response. |

Example request:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/run-live -ContentType "application/json" -Body '{"query":"SELECT id, name FROM customers WHERE city = ''Mumbai'' LIMIT 5"}'
```

`/api/run-live` responds with HTTP 400 for rejected SQL and HTTP 503 when MySQL is unavailable. MySQL query errors such as a missing column return HTTP 200 with `ok: false`, so the wizard can render an error node and request an AI suggestion.

## Tests

Run the Python suite from the repository root:

```powershell
pytest -v
```

Run the live end-to-end checks separately while the server is running. They require the seeded MySQL schema and a working Gemini free-tier key, make no more than two AI requests, and wait seven seconds between those requests:

```powershell
python test_e2e.py
```

The E2E script checks successful rows and plans, write-query rejection, SQL errors, dead ends, and Gemini responses. It exits with status 1 when the server, database, or real Gemini service is unavailable.
