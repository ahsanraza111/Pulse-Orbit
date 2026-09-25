from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from pulse.core.config import Settings

DatabaseProbe = Callable[[], Awaitable[bool]]


def create_http_app(
    settings: Settings,
    database_probe: DatabaseProbe | None = None,
) -> FastAPI:
    app = FastAPI(
        title="PULSE",
        version="0.1.0",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def readiness() -> JSONResponse:
        database_connected = (
            await database_probe()
            if settings.database_is_configured and database_probe is not None
            else False
        )
        database_requirement_ready = (
            not settings.orbit_provider_is_configured or database_connected
        )
        ready = (
            settings.teams_is_configured
            and settings.groq_is_configured
            and database_requirement_ready
        )
        payload = {
            "status": "ready" if ready else "not_ready",
            "teams_configured": settings.teams_is_configured,
            "groq_configured": settings.groq_is_configured,
            "orbit_configured": settings.orbit_is_configured,
            "database_configured": settings.database_is_configured,
            "database_connected": database_connected,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)
    return app
