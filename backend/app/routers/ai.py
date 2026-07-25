from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="/api", tags=["ai"])


@router.get("/ai/status")
def ai_status() -> dict[str, object]:
    """Report whether the OpenAI integration is configured, without leaking the key."""
    settings = get_settings()
    return {
        "configured": bool(settings.openai_api_key),
        "model": settings.llm_model_extract,
    }
