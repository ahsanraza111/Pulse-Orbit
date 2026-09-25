import logging

from pulse.core.logging import configure_logging


def test_teams_activity_payload_logging_is_never_debug() -> None:
    configure_logging("DEBUG")

    assert logging.getLogger("microsoft_teams.apps.app_process").getEffectiveLevel() >= logging.INFO
