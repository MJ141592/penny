# syntax=docker/dockerfile:1
# One image, two stages. The SPA is built with node and then served by FastAPI from the
# same origin as /api, which is what deletes CORS, SameSite=None and CSRF. That is why the
# frontend has no container of its own.
#
# No migrations here: Railway's private networking is RUNTIME ONLY, so the database is
# unreachable at build time. `alembic upgrade head` runs in railway.json's preDeployCommand.

# ---------- stage 1: build the SPA ----------
FROM node:22-alpine AS frontend

WORKDIR /build

# Manifests first: `npm ci` is the slow layer and only package*.json should invalidate it.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# `tsc -b && vite build` — a type error under strict fails the image, not the deploy.
RUN npm run build

# ---------- stage 2: the app ----------
FROM python:3.12-slim AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000

WORKDIR /app

# uv comes from its own image at a pinned tag, matching the uv that wrote uv.lock —
# `:latest` is a moving target that can change resolution between two builds of the same
# commit. It is *mounted*, not COPYed: a build tool has no business in the runtime image,
# and it is 50 MB. Nothing at runtime needs it, including `alembic upgrade head`, because
# the venv is on PATH.
#
# Dependencies before app code, so editing a router does not reinstall SQLAlchemy.
# --no-install-project is what makes that split possible: without it uv needs app/ present.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.19,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.19,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Beside the app package, where app/spa.py looks first (/app/static).
COPY --from=frontend /build/dist ./static

# Non-root. Nothing under /app is written at runtime, so root-owned files are correct:
# the app cannot modify its own code or dependencies. The home directory is not optional
# — gunicorn's control server opens a socket under $HOME and logs an ERROR on every boot
# without one, which is exactly the kind of noise that hides a real failure.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin penny
USER penny

EXPOSE 8000

# Mirrors railway.json's startCommand so `docker run` behaves like the deploy.
#   gunicorn, not uvicorn — uvicorn cannot dual-stack bind; `--host ::` is IPv6-only and
#     breaks Railway's public healthcheck. `[::]` must keep its brackets: a bare `::`
#     yields ":::8000" and fails net.SplitHostPort.
#   --workers 2 — user-triggered extraction runs in FastAPI BackgroundTasks, in-process.
#     With one worker a long import blocks every other request, including the healthcheck.
#     Two fits the ~250 MB this service is budgeted for; more buys nothing at 1-5 families.
#   --timeout 120 — gunicorn's 30 s default kills a worker whose event loop is busy
#     parsing a 25 MB export, which is a normal request here, not a hang.
CMD ["sh", "-c", "exec gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind [::]:$PORT --workers 2 --timeout 120"]
