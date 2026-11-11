from __future__ import annotations

from pulse.application.errors import ReminderDeliveryError
from pulse.application.ports.reminders import ReminderDelivery


class ReminderDispatcher:
    """Late-bound reminder sender.

    The composition root builds the reminder service before the Teams app exists, so the
    Teams delivery function is bound afterwards by the process entry point.
    """

    def __init__(self) -> None:
        self._deliver: ReminderDelivery | None = None

    def bind(self, deliver: ReminderDelivery) -> None:
        self._deliver = deliver

    async def send_reminder(self, conversation_id: str, service_url: str) -> None:
        if self._deliver is None:
            raise ReminderDeliveryError("Reminder delivery is not connected to Teams yet.")
        try:
            await self._deliver(conversation_id, service_url)
        except ReminderDeliveryError:
            raise
        except Exception as exc:
            raise ReminderDeliveryError("The Teams reminder could not be delivered.") from exc
