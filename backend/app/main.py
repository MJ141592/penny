from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings

app = FastAPI(title="Penny API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ai/status")
def ai_status() -> dict[str, object]:
    """Report whether the OpenAI integration is configured, without leaking the key."""
    settings = get_settings()
    return {
        "configured": bool(settings.openai_api_key),
        "model": settings.openai_model,
    }
