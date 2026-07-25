from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import ai, health

settings = get_settings()

app = FastAPI(title="Penny API")

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

app.include_router(health.router)
app.include_router(ai.router)
# Each later track adds exactly one include_router line here — auth, feed, import,
# reports, webhooks, internal. Keep the route bodies in app/routers/.
