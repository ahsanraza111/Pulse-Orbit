from __future__ import annotations

import json
from datetime import date
from typing import cast

from pulse.application.errors import OrbitValidationError
from pulse.application.models import ChatMessage
from pulse.application.orbit_models import (
    BulkSchedule,
    ParsedBulkEntryTemplate,
    ParsedBulkTimesheetRequest,
)
from pulse.application.ports.llm import LLMClient


class LLMTimesheetBatchParser:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def parse(self, text: str, *, today: date) -> ParsedBulkTimesheetRequest:
        response = await self._llm.complete(
            (
                ChatMessage(
                    role="system",
                    content=(
                        "Extract an Orbit bulk-timesheet request from the user's message. "
                        f"Today's business date is {today.isoformat()}. Resolve relative dates "
                        "against that date. Return JSON only with exactly these top-level keys: "
                        '"start_date" (YYYY-MM-DD), "end_date" (YYYY-MM-DD), "schedule", '
                        'and "entries". Schedule must be "single_day" when start and end are '
                        'the same, "weekdays" only when the user explicitly requests weekdays '
                        'or working days, "all_days" only when the user explicitly requests '
                        "every/calendar day including weekends, otherwise null for a multi-day "
                        "range. Entries must be an array with exactly these keys per item: "
                        '"project_name", "task_name", "duration_minutes" (integer), and '
                        '"description". Use null for missing values. Do not invent details.'
                    ),
                ),
                ChatMessage(role="user", content=text.strip()),
            )
        )
        payload = self._parse_json_object(response)
        try:
            start_date = date.fromisoformat(str(payload["start_date"]))
            end_date = date.fromisoformat(str(payload["end_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise OrbitValidationError(
                "Please provide a valid bulk-entry date or date range."
            ) from exc

        schedule_value = payload.get("schedule")
        allowed_schedules = {"single_day", "weekdays", "all_days"}
        if schedule_value is not None and schedule_value not in allowed_schedules:
            raise OrbitValidationError("The bulk-entry repeat schedule could not be understood.")
        schedule = cast(BulkSchedule | None, schedule_value)

        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise OrbitValidationError("Provide at least one complete timesheet entry.")

        entries: list[ParsedBulkEntryTemplate] = []
        for index, raw_entry in enumerate(raw_entries, start=1):
            if not isinstance(raw_entry, dict):
                raise OrbitValidationError(f"Bulk entry {index} could not be understood.")
            missing = [
                key
                for key in (
                    "project_name",
                    "task_name",
                    "duration_minutes",
                    "description",
                )
                if raw_entry.get(key) in (None, "")
            ]
            if missing:
                raise OrbitValidationError(
                    f"Bulk entry {index} is missing: " + ", ".join(missing) + "."
                )
            duration_value = raw_entry["duration_minutes"]
            if isinstance(duration_value, bool):
                raise OrbitValidationError(f"Bulk entry {index} has an invalid duration.")
            try:
                duration_minutes = int(duration_value)
            except (TypeError, ValueError) as exc:
                raise OrbitValidationError(
                    f"Bulk entry {index} has an invalid duration."
                ) from exc
            entries.append(
                ParsedBulkEntryTemplate(
                    project_name=str(raw_entry["project_name"]).strip(),
                    task_name=str(raw_entry["task_name"]).strip(),
                    duration_minutes=duration_minutes,
                    description=str(raw_entry["description"]).strip(),
                )
            )

        return ParsedBulkTimesheetRequest(
            start_date=start_date,
            end_date=end_date,
            schedule=schedule,
            entries=tuple(entries),
        )

    @staticmethod
    def _parse_json_object(response: str) -> dict[str, object]:
        start, end = response.find("{"), response.rfind("}")
        if start < 0 or end <= start:
            raise OrbitValidationError(
                "The bulk entry details could not be parsed. Please rephrase."
            )
        try:
            payload = json.loads(response[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OrbitValidationError(
                "The bulk entry details could not be parsed. Please rephrase."
            ) from exc
        if not isinstance(payload, dict):
            raise OrbitValidationError(
                "The bulk entry details could not be parsed. Please rephrase."
            )
        return payload
