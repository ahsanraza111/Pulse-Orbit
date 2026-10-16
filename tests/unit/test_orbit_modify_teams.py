import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from pulse.application.errors import OrbitMutationUncertainError, OrbitValidationError
from pulse.application.orbit_models import (
    AppliedEntryChange,
    EditedEntryValues,
    EditFormContext,
    OrbitProject,
    OrbitTimesheetEntry,
    PendingEntryChange,
)
from pulse.presentation.teams import (
    OrbitChangeCancelCardHandler,
    OrbitChangeConfirmCardHandler,
    OrbitDeleteStartCardHandler,
    OrbitEditStartCardHandler,
    OrbitEditSubmitCardHandler,
    orbit_delete_confirmation_card,
    orbit_edit_form_card,
    orbit_timesheet_detail_card,
    orbit_update_confirmation_card,
)


class FakeCardContext:
    def __init__(self, data: dict[str, object], activity_id: str = "card-1") -> None:
        self.activity = SimpleNamespace(
            id=activity_id,
            from_=SimpleNamespace(id="teams-user-1", aad_object_id="entra-user-1"),
            value=SimpleNamespace(action=SimpleNamespace(data=data)),
        )
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)


def draft_entry() -> OrbitTimesheetEntry:
    return OrbitTimesheetEntry(
        id="3c160089-5f25-4bab-a722-5179c0e5d3ae",
        entry_date=date(2026, 9, 25),
        duration_minutes=90,
        description="Fixed validation",
        status="draft",
        project_name="ADGM Form Submissions",
        task_name="Backend Development",
        project_id="project-1",
        task_id="task-1",
    )


def pending_change(kind: str = "update") -> PendingEntryChange:
    after = (
        EditedEntryValues(
            project_id="project-1",
            project_name="ADGM Form Submissions",
            task_id="task-1",
            task_name="Backend Development",
            entry_date=date(2026, 9, 25),
            duration_minutes=120,
            description="Fixed validation",
        )
        if kind == "update"
        else None
    )
    return PendingEntryChange(
        id="change-1",
        kind=kind,  # type: ignore[arg-type]
        teams_user_id="entra-user-1",
        employee_id="employee-1",
        organization_id="organization-1",
        before=draft_entry(),
        after=after,
        expires_at=datetime(2026, 9, 25, 13, tzinfo=UTC),
    )


class FakeOrbitModifyService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.error: Exception | None = None

    async def start_edit(self, teams_user_id: str, entry_id: str) -> EditFormContext:
        self.calls.append(("start_edit", (teams_user_id, entry_id)))
        if self.error:
            raise self.error
        return EditFormContext(
            draft_entry(), (OrbitProject("project-1", "ADGM Form Submissions"),)
        )

    async def prepare_edit(self, teams_user_id: str, entry_id: str, **fields: str):
        self.calls.append(("prepare_edit", (teams_user_id, entry_id, fields)))
        if self.error:
            raise self.error
        return pending_change()

    async def prepare_delete(self, teams_user_id: str, entry_id: str):
        self.calls.append(("prepare_delete", (teams_user_id, entry_id)))
        return pending_change("delete")

    async def confirm(self, teams_user_id: str, change_id: str) -> AppliedEntryChange:
        self.calls.append(("confirm", (teams_user_id, change_id)))
        if self.error:
            raise self.error
        return AppliedEntryChange(kind="delete", entry=draft_entry())

    async def cancel(self, teams_user_id: str, change_id: str) -> None:
        self.calls.append(("cancel", (teams_user_id, change_id)))


def card_text(value: object) -> str:
    return str(value.model_dump(by_alias=True, exclude_none=True))  # type: ignore[attr-defined]


def test_draft_detail_card_offers_edit_and_delete_but_locked_entries_do_not() -> None:
    draft_card = card_text(orbit_timesheet_detail_card(draft_entry()))
    locked_card = card_text(
        orbit_timesheet_detail_card(replace(draft_entry(), status="approved"))
    )

    assert "orbit_edit_start" in draft_card
    assert "orbit_delete_start" in draft_card
    assert "orbit_edit_start" not in locked_card
    assert "orbit_delete_start" not in locked_card


def test_edit_form_prefills_current_values() -> None:
    context = EditFormContext(
        draft_entry(), (OrbitProject("project-1", "ADGM Form Submissions"),)
    )

    text = card_text(orbit_edit_form_card(context))

    for expected in ("edit_date", "2026-09-25", "1h 30m", "edit_project", "edit_task"):
        assert expected in text
    assert "orbit_edit_submit" in text


def test_update_confirmation_card_shows_before_and_after_and_requires_click() -> None:
    text = card_text(orbit_update_confirmation_card(pending_change()))

    assert "1h 30m → 2h 00m" in text
    assert "orbit_change_confirm" in text
    assert "change-1" in text
    assert "not updated until" in text


def test_delete_confirmation_card_shows_entry_and_requires_click() -> None:
    text = card_text(orbit_delete_confirmation_card(pending_change("delete")))

    assert "permanently deleted" in text
    assert "Backend Development" in text
    assert "orbit_change_confirm" in text


@pytest.mark.asyncio
async def test_edit_button_sends_prefilled_form_once_per_submission() -> None:
    modify = FakeOrbitModifyService()
    context = FakeCardContext(
        {"action": "orbit_edit_start", "entry_id": "entry-1"}, activity_id="edit-start-1"
    )
    handler = OrbitEditStartCardHandler(modify)  # type: ignore[arg-type]

    response = await handler(context)
    await handler(context)
    await asyncio.sleep(0)

    assert "Opening edit form" in card_text(response.value)
    assert modify.calls == [("start_edit", ("entra-user-1", "entry-1"))]
    assert len(context.sent) == 1
    assert "Edit Orbit entry" in card_text(context.sent[0])


@pytest.mark.asyncio
async def test_edit_submit_passes_form_fields_and_posts_confirmation_card() -> None:
    modify = FakeOrbitModifyService()
    context = FakeCardContext(
        {
            "action": "orbit_edit_submit",
            "entry_id": "entry-1",
            "edit_date": "2026-09-25",
            "edit_duration": "2h",
            "edit_notes": "Fixed validation",
            "edit_project": "project-1",
            "edit_task": "Backend Development",
        },
        activity_id="edit-submit-1",
    )

    await OrbitEditSubmitCardHandler(modify)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert modify.calls == [
        (
            "prepare_edit",
            (
                "entra-user-1",
                "entry-1",
                {
                    "entry_date_text": "2026-09-25",
                    "duration_text": "2h",
                    "description": "Fixed validation",
                    "project_id": "project-1",
                    "task_name": "Backend Development",
                },
            ),
        )
    ]
    assert "Confirm Orbit entry update" in card_text(context.sent[0])


@pytest.mark.asyncio
async def test_edit_submit_validation_error_is_posted_as_status_card() -> None:
    modify = FakeOrbitModifyService()
    modify.error = OrbitValidationError("No changes were made to this entry.")
    context = FakeCardContext(
        {"action": "orbit_edit_submit", "entry_id": "entry-1"}, activity_id="edit-submit-2"
    )

    await OrbitEditSubmitCardHandler(modify)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert "No changes were made" in card_text(context.sent[0])


@pytest.mark.asyncio
async def test_delete_button_posts_confirmation_without_deleting() -> None:
    modify = FakeOrbitModifyService()
    context = FakeCardContext(
        {"action": "orbit_delete_start", "entry_id": "entry-1"}, activity_id="delete-start-1"
    )

    await OrbitDeleteStartCardHandler(modify)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert [name for name, _ in modify.calls] == ["prepare_delete"]
    assert "Confirm Orbit entry deletion" in card_text(context.sent[0])


@pytest.mark.asyncio
async def test_change_confirm_applies_once_even_when_delivered_twice() -> None:
    modify = FakeOrbitModifyService()
    context = FakeCardContext(
        {"action": "orbit_change_confirm", "change_id": "change-1"}, activity_id="confirm-1"
    )
    handler = OrbitChangeConfirmCardHandler(modify)  # type: ignore[arg-type]

    response = await handler(context)
    await handler(context)
    await asyncio.sleep(0)

    assert "Applying Orbit change" in card_text(response.value)
    assert modify.calls == [("confirm", ("entra-user-1", "change-1"))]
    assert len(context.sent) == 1
    assert "Orbit entry deleted" in card_text(context.sent[0])


@pytest.mark.asyncio
async def test_change_confirm_reports_uncertain_outcome_for_verification() -> None:
    modify = FakeOrbitModifyService()
    modify.error = OrbitMutationUncertainError("Check Orbit before retrying.")
    context = FakeCardContext(
        {"action": "orbit_change_confirm", "change_id": "change-1"}, activity_id="confirm-2"
    )

    await OrbitChangeConfirmCardHandler(modify)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    text = card_text(context.sent[0])
    assert "needs verification" in text
    assert "Check Orbit" in text


@pytest.mark.asyncio
async def test_change_cancel_discards_without_mutating() -> None:
    modify = FakeOrbitModifyService()
    context = FakeCardContext({"action": "orbit_change_cancel", "change_id": "change-1"})

    response = await OrbitChangeCancelCardHandler(modify)(context)  # type: ignore[arg-type]

    assert modify.calls == [("cancel", ("entra-user-1", "change-1"))]
    assert "No change was sent to Orbit" in card_text(response.value)
