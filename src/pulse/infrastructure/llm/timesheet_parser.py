from __future__ import annotations

import json
from datetime import date

from pulse.application.errors import OrbitValidationError
from pulse.application.models import ChatMessage
from pulse.application.orbit_models import ParsedTimesheetDraft
from pulse.application.ports.llm import LLMClient


class LLMTimesheetDraftParser:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def parse(self, text: str, *, today: date) -> ParsedTimesheetDraft:
        response = await self._llm.complete(
            (
                ChatMessage(
                    role="system",
                    content=(
                        "Extract an Orbit timesheet draft from the user's message. "
                        f"Today's business date is {today.isoformat()}. Resolve relative dates "
                        "against that date. Return JSON only with exactly these keys: "
                        '"project_name", "task_name", "entry_date" (YYYY-MM-DD), '
                        '"duration_minutes" (integer), and "description". Use null for any '
                        "missing value. Do not invent a project, task, duration, date, or notes."
                    ),
                ),
                ChatMessage(role="user", content=text.strip()),
            )
        )
        payload = self._parse_json_object(response)
        missing = [
            key
            for key in (
                "project_name",
                "task_name",
                "entry_date",
                "duration_minutes",
                "description",
            )
            if payload.get(key) in (None, "")
        ]
        if missing:
            raise OrbitValidationError(
                "Please provide the missing entry details: " + ", ".join(missing) + "."
            )
        try:
            entry_date = date.fromisoformat(str(payload["entry_date"]))
            duration_minutes = int(payload["duration_minutes"])
        except (TypeError, ValueError) as exc:
            raise OrbitValidationError(
                "Orbit entry date or duration could not be understood."
            ) from exc

        return ParsedTimesheetDraft(
            project_name=str(payload["project_name"]).strip(),
            task_name=str(payload["task_name"]).strip(),
            entry_date=entry_date,
            duration_minutes=duration_minutes,
            description=str(payload["description"]).strip(),
        )

    @staticmethod
    def _parse_json_object(response: str) -> dict[str, object]:
        start, end = response.find("{"), response.rfind("}")
        if start < 0 or end <= start:
            raise OrbitValidationError("The entry details could not be parsed. Please rephrase.")
        try:
            payload = json.loads(response[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OrbitValidationError(
                "The entry details could not be parsed. Please rephrase."
            ) from exc
        if not isinstance(payload, dict):
            raise OrbitValidationError("The entry details could not be parsed. Please rephrase.")
        return payload
