from __future__ import annotations

import asyncio

import uvicorn

from pulse.bootstrap import Container
from pulse.core.config import get_settings
from pulse.core.logging import configure_logging
from pulse.presentation.http import create_http_app
from pulse.presentation.teams import register_teams_app


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = Container.build(settings)

    fastapi_app = create_http_app(
        settings,
        container.database.is_ready if container.database else None,
    )
    teams_app = register_teams_app(
        fastapi_app,
        settings,
        container.chat_service,
        container.orbit_auth_service,
        container.orbit_add_service,
        container.orbit_view_service,
    )
    await teams_app.initialize()

    server = uvicorn.Server(
        uvicorn.Config(
            app=fastapi_app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
        )
    )
    try:
        await server.serve()
    finally:
        await container.close()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
