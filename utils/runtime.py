from __future__ import annotations

import logging
from pathlib import Path


def setup_app_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("kiln_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def format_trend(current_temp_f: float, previous_temp_f: float | None) -> str:
    if previous_temp_f is None:
        return "steady"

    delta = current_temp_f - previous_temp_f
    if abs(delta) < 0.5:
        return "steady"
    if delta > 0:
        return "rising"
    return "falling"
