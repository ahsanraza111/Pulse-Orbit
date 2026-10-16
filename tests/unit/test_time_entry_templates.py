from pulse.presentation.teams import (
    TIME_ENTRY_TEMPLATES,
    time_entry_help_messages,
    time_entry_templates_card,
)


def dump(value: object) -> dict:
    return value.model_dump(by_alias=True, exclude_none=True)  # type: ignore[attr-defined]


def test_templates_card_shows_each_title_with_its_copyable_template() -> None:
    card = dump(time_entry_templates_card())
    text = str(card)

    for _, title, command in TIME_ENTRY_TEMPLATES:
        assert title in text
        for line in command.split("\n"):
            assert line in text
    assert "Monospace" in text


def test_templates_card_has_no_buttons() -> None:
    card = dump(time_entry_templates_card())

    assert "actions" not in card
    assert "Action." not in str(card)


def test_range_template_is_shown_on_two_lines() -> None:
    text = str(dump(time_entry_templates_card()))

    assert "log these entries from this [from date] to [to date]" in text
    assert text.count("add 1 hour to [project name]") == 4


def test_help_messages_send_card_first_and_compose_suggestions_last() -> None:
    card, suggestions = time_entry_help_messages(newly_connected=False)

    assert "Time-entry templates" in str(dump(card))
    payload = dump(suggestions)
    assert "already signed in" in payload["text"]
    assert len(payload["suggestedActions"]["actions"]) == 4
