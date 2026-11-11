from __future__ import annotations

import asyncio

import uvicorn

from pulse.bootstrap import Container
from pulse.core.config import get_settings
from pulse.core.logging import configure_logging
from pulse.infrastructure.reminders.scheduler import ReminderScheduler
from pulse.presentation.http import create_http_app
from pulse.presentation.teams import register_teams_app, teams_reminder_delivery


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = Container.build(settings)

    fastapi_app = create_http_app(
        settings,
        container.database.is_ready if container.database else None,
        container.reminder_service.run_due if container.reminder_service else None,
    )
    teams_app = register_teams_app(
        fastapi_app,
        settings,
        container.chat_service,
        container.orbit_auth_service,
        container.orbit_add_service,
        container.orbit_view_service,
        container.orbit_bulk_service,
        container.orbit_modify_service,
        container.reminder_service,
    )
    await teams_app.initialize()
    if container.reminder_dispatcher:
        container.reminder_dispatcher.bind(teams_reminder_delivery(teams_app))
    scheduler_task: asyncio.Task[None] | None = None
    if container.reminder_service and settings.reminder_scheduler_enabled:
        scheduler_task = asyncio.create_task(
            ReminderScheduler(
                container.reminder_service.run_due,
                interval_seconds=settings.reminder_poll_seconds,
            ).run()
        )

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
        if scheduler_task:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        await container.close()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
