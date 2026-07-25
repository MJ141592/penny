# Penny

Web app with a Python (FastAPI) backend and a TypeScript React (Vite) frontend.

## Structure

- `backend/` — FastAPI app (Python 3.11+)
- `frontend/` — React + TypeScript, built with Vite

## Environment

```sh
cp .env.example .env   # then fill in your real keys
```

`.env` is gitignored — only `.env.example` is committed. The backend reads it via
`app.config.get_settings()`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | yes | OpenAI auth; without it `get_openai_client()` raises |
| `OPENAI_MODEL` | no | Default model (`gpt-4o-mini`) |
| `OPENAI_BASE_URL` | no | Override the API base for proxies/gateways |

Use the client from request handlers:

```python
from app.openai_client import get_openai_client

client = get_openai_client()  # cached AsyncOpenAI instance
```

`GET /api/ai/status` reports whether the key is configured (it never returns the key).

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
