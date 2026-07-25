# Penny

Web app with a Python (FastAPI) backend and a TypeScript React (Vite) frontend.

## Structure

- `backend/` — FastAPI app (Python 3.11+)
- `frontend/` — React + TypeScript, built with Vite

## Backend

```sh
cd backend
uv sync            # or: python -m venv .venv && .venv/bin/pip install -e . --group dev
uv run uvicorn app.main:app --reload
```

The API runs at http://localhost:8000 (health check: `GET /api/health`).

Run tests:

```sh
uv run pytest
```

## Frontend

```sh
cd frontend
npm install
npm run dev
```

The dev server runs at http://localhost:5173 and proxies `/api` requests to the backend.
