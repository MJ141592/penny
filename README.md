# Penny

Families caring for an elderly parent lose the thread of shared care: nobody has a record of who did
what, what happened at the appointment, or when the dizziness started. The family already coordinates
in WhatsApp, so Penny reads that conversation, extracts structured care events, and renders a
browsable history feed, upcoming appointments, and periodic LLM care-management reports.

A website, not a mobile app. Pre-product, aimed at 1–5 test families of ~3–8 members — optimised for a
demoable vertical slice, not for scale.

## Layout

```
backend/           FastAPI app (Python 3.12+), managed with uv
  app/             config, OpenAI client, routers
  tests/           pytest; `live` and `db` markers opt in to external deps
frontend/          React + TypeScript, built with Vite
infra/             provision.py — the whole deployment, as one idempotent script
docs/              API contract, deployment, GOWA runbook
```

Two deployed services plus Postgres: `penny` (FastAPI, which also serves `frontend/dist` so the app is
same-origin) and `gowa` (the WhatsApp bridge). See [docs/railway-deployment.md](docs/railway-deployment.md).

## Environment

```sh
cp .env.example .env   # then fill in your real keys
```

`.env` is gitignored — only `.env.example` is committed. The backend reads it via
`app.config.get_settings()`, from the repo root or from `backend/`.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ENV` | no | `dev` | `dev` \| `test` \| `production`. Anything but `production` enables the dev-only CORS middleware |
| `OPENAI_API_KEY` | yes | — | OpenAI auth; without it `get_openai_client()` raises |
| `OPENAI_BASE_URL` | no | — | Override the API base for proxies/gateways |
| `LLM_MODEL_EXTRACT` | no | `gpt-5.5-2026-04-23` | Extraction model. Dated snapshot, never the floating alias |
| `LLM_MODEL_REPORT` | no | `gpt-5.5-2026-04-23` | Report model |
| `DATABASE_URL` | in deploy | — | Postgres. `postgres://` / `postgresql://` are rewritten to `postgresql+asyncpg://` for you |
| `PENNY_TEST_DATABASE_URL` | no | — | Postgres URL for the `db`-marked tests; they skip when unset |
| `SESSION_SECRET` | in deploy | — | Signs the HttpOnly cookie carrying `household_id` |
| `SESSION_MAX_AGE_DAYS` | no | `30` | Session cookie lifetime |
| `GOWA_URL` | M6 | — | Private URL of the WhatsApp sidecar, e.g. `http://gowa.railway.internal:3000` |
| `GOWA_BASIC_AUTH` | M6 | — | `user:pass` for calling GOWA; must match its `APP_BASIC_AUTH` |
| `WHATSAPP_WEBHOOK_SECRET` | M6 | — | HMAC-SHA256 key for inbound webhooks; must match GOWA's |
| `INTERNAL_TICK_SECRET` | M5 | — | Shared secret for the cron caller of `/api/internal/tick` |
| `IMPORT_MAX_SPEND_USD` | no | `25` | Hard ceiling per import; aborts mid-flight and resumes later |
| `LLM_MONTHLY_BUDGET_USD_PER_HOUSEHOLD` | no | `15` | Per-household monthly spend guard |
| `DEFAULT_TIMEZONE` | no | `Europe/London` | Default household timezone |
| `SERVE_FRONTEND` | no | `true` | Mount `frontend/dist` from the API process (how production serves the SPA) |
| `ONBOARDING_ENABLED` | no | `true` | Adding Penny to a WhatsApp group provisions that group's household and posts the credentials into the group. `false` restores the old silent 200 — the kill switch if the number gets out |
| `ONBOARDING_MAX_HOUSEHOLDS` | no | `25` | Ceiling on onboarding. The exposure is the OpenAI bill, not data: every household runs extraction |
| `APP_PUBLIC_URL` | no | `https://pennyai.chat` | The URL in the welcome message. Wrong value = a dead link in the first thing a family ever sees |

Test-only, read by the suite rather than by `Settings`: `RUN_LIVE_OPENAI_TESTS=1` opts in to the
`live` tests.

The model ids default to a **dated snapshot** rather than the floating alias, because a silent model
swap changes extraction quality with no diff to point at — and because the call conventions are
version-specific: `reasoning.effort` ∈ `{none,low,medium,high,xhigh}`, `max_output_tokens`, and never
`temperature` / `top_p` / `max_tokens`.

Which of these each deployed service needs is documented per-service in
[docs/railway-deployment.md](docs/railway-deployment.md).

Use the client from request handlers:

```python
from app.openai_client import get_openai_client

client = get_openai_client()  # cached AsyncOpenAI instance
```

`GET /api/ai/status` reports whether the key is configured (it never returns the key).

## Backend

```sh
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

The API runs at http://localhost:8000 (health check: `GET /api/health`).

## Frontend

```sh
cd frontend
npm install
npm run dev
```

The dev server runs at http://localhost:5173 and proxies `/api` to the backend, matching the
same-origin arrangement in production. Checks:

The landing page defaults to Penny's production WhatsApp number. For another environment, copy
`frontend/.env.example` to `frontend/.env` and set `VITE_PENNY_WHATSAPP_NUMBER` to digits in E.164
order.

```sh
npm run build       # tsc -b && vite build
npm run lint        # oxlint
```

## Tests

```sh
cd backend
uv run pytest                     # default: live and db tests skip
```

Two markers gate the tests that need something external. Both skip silently when their variable is
unset, so a clean checkout stays runnable:

```sh
RUN_LIVE_OPENAI_TESTS=1 uv run pytest -m live      # hits the real OpenAI API; costs a fraction of a cent
PENNY_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest -m db
```

CI runs `pytest -m "not live"` plus the frontend build on every PR.

## Deploying

The deployment is reproducible from this repository. One idempotent script builds all of it — the
project, Postgres, both services, the volume, every generated secret and the domain:

```sh
export OPENAI_API_KEY=sk-...
infra/provision.py --workspace <workspace-id> --domain penny.example.com --dry-run   # look first
infra/provision.py --workspace <workspace-id> --domain penny.example.com
```

An OpenAI key and a domain are the only two values a teammate has to bring. Everything else is
generated per environment, derived from the project's topology, or a default in
`backend/app/config.py` that is deliberately **not** set on Railway.

Re-running converges: nothing is duplicated and no existing secret is rotated. `--self-test` proves
the invariants offline, `--dry-run` shows every mutation before it happens.

Full runbook — prerequisites, the steps only a human can do, how to verify, how to roll back, the
kill switch, and the fourteen platform traps that cost a night — in
[docs/deployment.md](docs/deployment.md).

## Docs

- [docs/deployment.md](docs/deployment.md) — the deployment runbook: provisioning, verification, rollback, traps
- [infra/variables.md](infra/variables.md) — every variable, classified, and why it is allowed to exist
- [docs/architecture.md](docs/architecture.md) — end-to-end system and deployment architecture
- [docs/api-contract.md](docs/api-contract.md) — the HTTP contract the frontend types are generated from
- [docs/railway-deployment.md](docs/railway-deployment.md) — service topology, variables, and the platform gotchas
- [docs/gowa-runbook.md](docs/gowa-runbook.md) — pairing a WhatsApp account; needs a physical phone
