from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.errors import install_error_handlers
from app.headers import install_security_headers
from app.logging import configure_logging, make_request_log_middleware
from app.routers import (
    ai,
    auth,
    events,
    feed,
    health,
    household,
    imports,
    members,
    webhooks,
    whatsapp,
)
from app.spa import mount_spa
from app.startup_checks import enforce_startup_checks

settings = get_settings()

# Before the app is built, so anything logged during startup is already JSON and
# already redacted.
configure_logging()

# Fail CLOSED, at import, before anything binds a port. In production this refuses to boot on a
# secret still set to a placeholder published in this repository's .env.example — a public string
# that would let anyone forge a session cookie for any household. Warn-only outside production,
# or every contributor's first run breaks.
enforce_startup_checks(settings)

app = FastAPI(title="Penny API")

install_error_handlers(app)
app.middleware("http")(make_request_log_middleware())

# Dev only: production serves the SPA from this same origin, so CORS is dead weight
# there and an unnecessary way to widen the surface.
if settings.env != "production":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Before the routers, so every response carries them — including error envelopes and the SPA.
install_security_headers(app, settings)

app.include_router(health.router)
app.include_router(ai.router)
app.include_router(auth.router)  # POST /api/auth/login, /logout; GET /api/me
app.include_router(feed.router)  # GET /api/feed, /api/upcoming
app.include_router(events.router)  # PATCH, DELETE /api/events/{id}
app.include_router(household.router)  # PATCH, DELETE /api/household; password change
app.include_router(members.router)  # GET /api/members; POST /api/members/{id}/merge
app.include_router(imports.router)  # POST /api/imports, /preview; GET /api/imports/{id}
app.include_router(whatsapp.router)  # GET /api/whatsapp/status; POST /link, /relink
app.include_router(webhooks.router)  # POST /api/whatsapp/webhook — HMAC, no cookie

# LAST, always. mount_spa registers a catch-all GET /{path:path}; any router included
# after it would be shadowed and its paths would return index.html instead.
mount_spa(app)
