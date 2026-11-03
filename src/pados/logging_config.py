# SPDX-License-Identifier: AGPL-3.0-only
import logging
import sys
from typing import Any, TextIO, cast

import structlog


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """JSON log events to ``stream`` (standard output by default, as the server has always logged).

    The CLI passes standard error, so a command's ``--json`` output on standard output stays one JSON document.
    """
    target = stream or sys.stdout
    logging.basicConfig(format="%(message)s", stream=target, level=getattr(logging, level.upper()), force=True)
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=target),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **context: Any) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name).bind(**context))
