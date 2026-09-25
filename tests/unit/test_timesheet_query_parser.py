from datetime import date

import pytest

from pulse.application.errors import OrbitValidationError
from pulse.infrastructure.llm.timesheet_query_parser import LLMTimesheetQueryParser


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    async def complete(self, messages):
        self.messages = messages
        return self.response


@pytest.mark.asyncio
async def test_query_parser_returns_typed_filters() -> None:
    llm = FakeLLM(
        '{"start_date":"2026-09-21","end_date":"2026-09-27",'
        '"project_name":"ADGM forms","status":"DRAFT"}'
    )

    result = await LLMTimesheetQueryParser(llm).parse(
        "show draft ADGM entries this week", today=date(2026, 9, 25)
    )

    assert result.start_date == date(2026, 9, 21)
    assert result.end_date == date(2026, 9, 27)
    assert result.project_name == "ADGM forms"
    assert result.status == "draft"
    assert "Weeks run Monday through Sunday" in llm.messages[0].content


@pytest.mark.asyncio
async def test_query_parser_rejects_invalid_dates() -> None:
    parser = LLMTimesheetQueryParser(
        FakeLLM(
            '{"start_date":"not-a-date","end_date":"2026-09-27",'
            '"project_name":null,"status":null}'
        )
    )

    with pytest.raises(OrbitValidationError, match="date range"):
        await parser.parse("show this week", today=date(2026, 9, 25))
