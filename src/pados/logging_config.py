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


LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "WARN": logging.WARNING,
              "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}


def resolve_log_level(level: object) -> tuple[int, str | None]:
    """The numeric level for a ``PADOS_LOG_LEVEL`` value, and a stable problem text when it is not a known level.

    An unknown value never stops a command: ``pados init`` and ``pados doctor`` must still run to repair the
    configuration. Logging falls back to INFO and the problem is reported (on standard error, and by ``doctor``).
    """
    name = str(level if level is not None else "").strip().upper()
    if not name:  # unset or blank (``PADOS_LOG_LEVEL=`` in a .env template) means the default
        return logging.INFO, None
    if name in LOG_LEVELS:
        return LOG_LEVELS[name], None
    # The value itself is not echoed: a mis-pasted value could be something that must not reach a log.
    return logging.INFO, ("PADOS_LOG_LEVEL_INVALID: the value is not one of DEBUG, INFO, WARNING, ERROR, CRITICAL; "
                          "logging at INFO")


def configure_logging(level: str = "INFO", *, to_stderr: bool = False) -> str | None:
    """JSON log events, to standard output by default (as the server has always logged).

    The CLI passes ``to_stderr=True`` so that a command's ``--json`` output on standard output stays one JSON
    document. The stream is resolved at each write, so a replaced or closed stream is never held on to. Returns the
    problem text when ``level`` is not a known level (logging then runs at INFO), after writing it to standard error.
    """
    numeric, problem = resolve_log_level(level)
    if problem is not None:
        print(problem, file=sys.stderr)
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
    return problem


def get_logger(name: str, **context: Any) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name).bind(**context))
