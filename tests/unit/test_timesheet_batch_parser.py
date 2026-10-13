from datetime import date

import pytest

from pulse.application.errors import OrbitValidationError
from pulse.infrastructure.llm.timesheet_batch_parser import LLMTimesheetBatchParser


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    async def complete(self, messages):
        self.messages = messages
        return self.response


@pytest.mark.asyncio
async def test_batch_parser_returns_typed_range_and_templates() -> None:
    llm = FakeLLM(
        """{
          "start_date": "2026-09-21",
          "end_date": "2026-09-30",
          "schedule": "weekdays",
          "entries": [
            {
              "project_name": "ADGM Website",
              "task_name": "Meetings and Calls",
              "duration_minutes": 30,
              "description": "Standup"
            },
            {
              "project_name": "ADGM Website",
              "task_name": "Backend Development",
              "duration_minutes": 120,
              "description": "API fix"
            }
          ]
        }"""
    )

    parsed = await LLMTimesheetBatchParser(llm).parse(
        "bulk request", today=date(2026, 9, 30)
    )

    assert parsed.start_date == date(2026, 9, 21)
    assert parsed.end_date == date(2026, 9, 30)
    assert parsed.schedule == "weekdays"
    assert len(parsed.entries) == 2
    assert parsed.entries[1].duration_minutes == 120
    assert "Do not invent details" in llm.messages[0].content


@pytest.mark.asyncio
async def test_batch_parser_preserves_missing_range_schedule_for_clarification() -> None:
    parser = LLMTimesheetBatchParser(
        FakeLLM(
            '{"start_date":"2026-09-21","end_date":"2026-09-30",'
            '"schedule":null,"entries":[{"project_name":"Alpha",'
            '"task_name":"API","duration_minutes":60,"description":"Fix"}]}'
        )
    )

    parsed = await parser.parse("range request", today=date(2026, 9, 30))

    assert parsed.schedule is None


@pytest.mark.asyncio
async def test_batch_parser_reports_entry_number_for_missing_fields() -> None:
    parser = LLMTimesheetBatchParser(
        FakeLLM(
            '{"start_date":"2026-09-21","end_date":"2026-09-21",'
            '"schedule":"single_day","entries":[{"project_name":"Alpha",'
            '"task_name":null,"duration_minutes":60,"description":"Fix"}]}'
        )
    )

    with pytest.raises(OrbitValidationError, match="Bulk entry 1 is missing: task_name"):
        await parser.parse("request", today=date(2026, 9, 30))
