from fastapi import FastAPI

from pulse.core.config import Settings


def create_http_app(settings: Settings) -> FastAPI:
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
    async def readiness() -> dict[str, str | bool]:
        ready = settings.teams_is_configured and settings.groq_is_configured
        return {
            "status": "ready" if ready else "not_ready",
            "teams_configured": settings.teams_is_configured,
            "groq_configured": settings.groq_is_configured,
            "orbit_configured": settings.orbit_is_configured,
        }
    return app
