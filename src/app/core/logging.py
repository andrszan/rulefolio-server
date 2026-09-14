import logging


def configure_logging(log_level: str) -> None:
    logging.getLogger("app").setLevel(log_level)
