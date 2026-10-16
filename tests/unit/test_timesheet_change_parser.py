from datetime import date

import pytest

from pulse.application.errors import OrbitValidationError
from pulse.infrastructure.llm.timesheet_change_parser import LLMTimesheetChangeParser


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    async def complete(self, messages):
        self.messages = messages
        return self.response


TODAY = date(2026, 9, 25)


@pytest.mark.asyncio
async def test_change_parser_returns_typed_edit_request() -> None:
    llm = FakeLLM(
        'Sure: {"action":"edit","start_date":"2026-09-24","end_date":"2026-09-24",'
        '"project_name":"ADGM forms","task_name":null,"notes_hint":"validation",'
        '"new_entry_date":null,"new_duration_minutes":180,"new_description":null,'
        '"new_project_name":null,"new_task_name":"Code Review"}'
    )

    result = await LLMTimesheetChangeParser(llm).parse(
        "kal wali ADGM entry 3 ghante kar do, task code review", today=TODAY
    )

    assert result.action == "edit"
    assert result.start_date == date(2026, 9, 24)
    assert result.project_name == "ADGM forms"
    assert result.notes_hint == "validation"
    assert result.new_duration_minutes == 180
    assert result.new_task_name == "Code Review"
    assert result.new_description is None
    assert "2026-09-25" in llm.messages[0].content


@pytest.mark.asyncio
async def test_change_parser_drops_change_fields_for_delete() -> None:
    llm = FakeLLM(
        '{"action":"delete","start_date":"2026-09-24","end_date":"2026-09-24",'
        '"project_name":null,"task_name":null,"notes_hint":null,'
        '"new_entry_date":"2026-10-01","new_duration_minutes":60,"new_description":"x",'
        '"new_project_name":"y","new_task_name":"z"}'
    )

    result = await LLMTimesheetChangeParser(llm).parse("delete yesterday", today=TODAY)

    assert result.action == "delete"
    assert result.new_entry_date is None
    assert result.new_duration_minutes is None
    assert result.new_description is None
    assert result.new_project_name is None
    assert result.new_task_name is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "no json here",
        "[1, 2]",
        '{"action":"archive"}',
        '{"action":"edit","start_date":"not-a-date"}',
        '{"action":"edit","new_duration_minutes":"60"}',
        '{"action":"edit","new_duration_minutes":true}',
    ],
)
async def test_change_parser_rejects_unusable_output(response: str) -> None:
    with pytest.raises(OrbitValidationError, match="could not be understood"):
        await LLMTimesheetChangeParser(FakeLLM(response)).parse("x", today=TODAY)
