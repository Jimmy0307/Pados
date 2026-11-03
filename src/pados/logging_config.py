# SPDX-License-Identifier: AGPL-3.0-only
import logging
import sys
from typing import Any, TextIO, cast

import structlog


class _CurrentStderr:
    """A file-like proxy for whatever ``sys.stderr`` is at each write (never a stream captured at configure time)."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: str = "INFO", *, to_stderr: bool = False) -> None:
    """JSON log events, to standard output by default (as the server has always logged).

    The CLI passes ``to_stderr=True`` so that a command's ``--json`` output on standard output stays one JSON
    document. The stream is resolved at each write, so a replaced or closed stream is never held on to.
    """
    numeric = getattr(logging, level.upper())
    logging.basicConfig(format="%(message)s", stream=sys.stderr if to_stderr else sys.stdout, level=numeric)
    # Always set the factory: structlog.configure keeps any key it is not given, so the default must be explicit.
    factory = (structlog.PrintLoggerFactory(file=cast(TextIO, _CurrentStderr())) if to_stderr
               else structlog.PrintLoggerFactory())
    structlog.configure(
        logger_factory=factory,
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **context: Any) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name).bind(**context))
