import logging
import os
from logging.handlers import RotatingFileHandler

BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))
LOG_FILE: str = os.path.join(BASE_DIR, "replica.log")


def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(process)d:%(thread)d | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            "%Y-%m-%d %H:%M:%S.%f %z",
        )
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
