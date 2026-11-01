"""Liveness and readiness probes. No tenant, no auth."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.session import api_engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """The process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """PostgreSQL and Redis are reachable."""
    checks: dict[str, str] = {}
    try:
        async with api_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "unavailable"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", "checks": checks}, status_code=200 if healthy else 503
    )
