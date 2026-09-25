from datetime import date

import pytest

from pulse.application.errors import OrbitValidationError
from pulse.infrastructure.llm.timesheet_parser import LLMTimesheetDraftParser


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response

    async def complete(self, messages):
        return self.response


@pytest.mark.asyncio
async def test_parser_maps_structured_json() -> None:
    parser = LLMTimesheetDraftParser(
        FakeLLM(
            '{"project_name":"Alpha","task_name":"API","entry_date":"2026-09-22",'
            '"duration_minutes":120,"description":"Fixed retries"}'
        )
    )

    result = await parser.parse("request", today=date(2026, 9, 22))

    assert result.project_name == "Alpha"
    assert result.duration_minutes == 120


@pytest.mark.asyncio
async def test_parser_rejects_missing_fields() -> None:
    parser = LLMTimesheetDraftParser(FakeLLM('{"project_name":"Alpha"}'))

    with pytest.raises(OrbitValidationError, match="missing entry details"):
        await parser.parse("request", today=date(2026, 9, 22))

