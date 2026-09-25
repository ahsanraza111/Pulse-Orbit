import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # This SDK logger includes the complete inbound activity at DEBUG level.
    # Adaptive Card submissions contain the transient Orbit password, so keep
    # this particular logger above DEBUG even when application debugging is on.
    logging.getLogger("microsoft_teams.apps.app_process").setLevel(logging.INFO)
