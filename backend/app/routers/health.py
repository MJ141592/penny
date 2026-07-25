from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only — this must never touch the database.

    Railway restarts a service whose healthcheck fails, so a DB-dependent probe
    turns a transient Postgres blip into a rollback loop.
    """
    return {"status": "ok"}
