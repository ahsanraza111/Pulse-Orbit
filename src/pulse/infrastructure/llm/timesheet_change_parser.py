from __future__ import annotations

import json
from datetime import date

from pulse.application.errors import OrbitValidationError
from pulse.application.models import ChatMessage
from pulse.application.orbit_models import ParsedEntryChangeRequest
from pulse.application.ports.llm import LLMClient

_PARSE_ERROR = "The entry change could not be understood. Please rephrase."


class LLMTimesheetChangeParser:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def parse(self, text: str, *, today: date) -> ParsedEntryChangeRequest:
        response = await self._llm.complete(
            (
                ChatMessage(
                    role="system",
                    content=(
                        "Extract an edit or delete of ONE existing Orbit timesheet entry from "
                        f"the user's message. Today's business date is {today.isoformat()}; "
                        "weeks run Monday through Sunday. Return JSON only with exactly these "
                        'keys: "action" ("edit" or "delete"); the SELECTOR keys '
                        '"start_date" and "end_date" (inclusive YYYY-MM-DD, the day or range '
                        'the existing entry is on), "project_name", "task_name", and '
                        '"notes_hint" (a word or phrase from the existing notes); and the '
                        'CHANGE keys "new_entry_date" (YYYY-MM-DD), "new_duration_minutes" '
                        '(integer), "new_description", "new_project_name", and '
                        '"new_task_name". The selector describes the entry as it is NOW; the '
                        "change keys describe only what the user wants it to become. Use null "
                        'for anything not stated and for every change key when action is '
                        '"delete". Do not invent values.'
                    ),
                ),
                ChatMessage(role="user", content=text.strip()),
            )
        )
        payload = self._parse_json_object(response)
        action = payload.get("action")
        if action not in {"edit", "delete"}:
            raise OrbitValidationError(_PARSE_ERROR)

        is_edit = action == "edit"
        return ParsedEntryChangeRequest(
            action=action,
            start_date=self._date(payload.get("start_date")),
            end_date=self._date(payload.get("end_date")),
            project_name=self._text(payload.get("project_name")),
            task_name=self._text(payload.get("task_name")),
            notes_hint=self._text(payload.get("notes_hint")),
            new_entry_date=self._date(payload.get("new_entry_date")) if is_edit else None,
            new_duration_minutes=self._minutes(payload.get("new_duration_minutes"))
            if is_edit
            else None,
            new_description=self._text(payload.get("new_description")) if is_edit else None,
            new_project_name=self._text(payload.get("new_project_name")) if is_edit else None,
            new_task_name=self._text(payload.get("new_task_name")) if is_edit else None,
        )

    @staticmethod
    def _text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _date(value: object) -> date | None:
        if value is None or value == "":
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise OrbitValidationError(_PARSE_ERROR) from exc

    @staticmethod
    def _minutes(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise OrbitValidationError(_PARSE_ERROR)
        return value

    @staticmethod
    def _parse_json_object(response: str) -> dict[str, object]:
        start, end = response.find("{"), response.rfind("}")
        if start < 0 or end <= start:
            raise OrbitValidationError(_PARSE_ERROR)
        try:
            payload = json.loads(response[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OrbitValidationError(_PARSE_ERROR) from exc
        if not isinstance(payload, dict):
            raise OrbitValidationError(_PARSE_ERROR)
        return payload
