from __future__ import annotations

import json
from datetime import date

from pulse.application.errors import OrbitValidationError
from pulse.application.models import ChatMessage
from pulse.application.orbit_models import ParsedTimesheetQuery
from pulse.application.ports.llm import LLMClient


class LLMTimesheetQueryParser:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def parse(self, text: str, *, today: date) -> ParsedTimesheetQuery:
        response = await self._llm.complete(
            (
                ChatMessage(
                    role="system",
                    content=(
                        "Extract read-only Orbit timesheet list filters from the user's message. "
                        f"Today's business date is {today.isoformat()}. Weeks run Monday through "
                        "Sunday. If no date is supplied, use the current week. Return JSON only "
                        'with exactly: "start_date", "end_date" (inclusive YYYY-MM-DD), '
                        '"project_name", and "status". Use null for an omitted project or status. '
                        "Allowed statuses are draft, submitted, approved, and rejected. Resolve "
                        "relative dates, but do not invent a project or status."
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
                "The timesheet date range could not be understood. Please rephrase."
            ) from exc

        project_value = payload.get("project_name")
        status_value = payload.get("status")
        return ParsedTimesheetQuery(
            start_date=start_date,
            end_date=end_date,
            project_name=(
                project_value.strip()
                if isinstance(project_value, str) and project_value.strip()
                else None
            ),
            status=(
                status_value.strip().casefold()
                if isinstance(status_value, str) and status_value.strip()
                else None
            ),
        )

    @staticmethod
    def _parse_json_object(response: str) -> dict[str, object]:
        start, end = response.find("{"), response.rfind("}")
        if start < 0 or end <= start:
            raise OrbitValidationError(
                "The timesheet filters could not be parsed. Please rephrase."
            )
        try:
            payload = json.loads(response[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OrbitValidationError(
                "The timesheet filters could not be parsed. Please rephrase."
            ) from exc
        if not isinstance(payload, dict):
            raise OrbitValidationError(
                "The timesheet filters could not be parsed. Please rephrase."
            )
        return payload
