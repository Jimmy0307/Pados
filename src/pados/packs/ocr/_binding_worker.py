# SPDX-License-Identifier: AGPL-3.0-only
"""Child-process entry point for operator engine bindings (launched only by ``_isolated.isolated_binding``).

Run as ``python -I -B -m pados.packs.ocr._binding_worker <limits> <module:function>``. Its FIRST action
(``main``) applies the resource limits named by ``<limits>`` (``_rlimit_exec.enter_limits``: address
space, CPU, no file growth, descriptors, no core, ``RLIMIT_NPROC = 0`` so it can start no process and
no thread, an unprivileged process and, on Linux, no-new-privileges). Only then does it read stdin
(one canonical JSON header line: ``media_type``, ``pages``, ``languages``; then the input bytes),
import the operator module and call the function::

    function(payload: bytes, *, media_type: str, pages: tuple[int, ...], languages: tuple[str, ...])
        -> Mapping  # ocr.engine-result/v1, validated by the parent

Every image or document decoder the binding uses therefore runs inside this bounded child, never in
the PADOS process. ``-I`` ignores ``PYTHONPATH``, the user site and the working directory: the
operator module must be installed into the PADOS environment's site-packages.

stdout carries exactly one canonical ASCII JSON reply, ``{"ok": true, "result": {...}}`` or
``{"ok": false, "code": <closed>}``; nothing else is written and no exception text leaves the process.
A binding reports a closed code by raising ``BindingRefusal(code)`` with a code in ``BINDING_CODES``;
anything else becomes ``ENGINE_FAILED``. Exit status: 0 = a reply was written, 3 = the memory budget was
exhausted (``MemoryError``), 4 = protocol violation, 117 = limits refused. Imports only the standard
library and the stdlib-only ``_rlimit_exec`` before the operator module.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pados.packs.ocr._rlimit_exec import EXIT_LIMITS_REFUSED, enter_limits

EXIT_OK, EXIT_BUDGET, EXIT_PROTOCOL = 0, 3, 4
MAX_HEADER_BYTES = 64 * 1024
TARGET_PATTERN = (r"[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,7}"
                  r":[A-Za-z_][A-Za-z0-9_]{0,63}")  # identical to _isolated.BINDING_TARGET (a test pins this)
_TARGET = re.compile(TARGET_PATTERN)
# Codes an operator binding may report itself; anything else becomes ENGINE_FAILED.
BINDING_CODES = frozenset({"ENGINE_FAILED", "ENGINE_TIMEOUT", "ENGINE_OUTPUT_INVALID", "ENGINE_OUTPUT_TOO_LARGE",
                           "ENGINE_BUDGET_EXCEEDED", "LANGUAGE_UNSUPPORTED", "MEDIA_TYPE_UNSUPPORTED",
                           "ADAPTER_UNAVAILABLE"})
BINDING_IMPORT_FAILED = "ENGINE_BINDING_IMPORT_FAILED"


class BindingRefusal(Exception):
    """Raised by an operator binding to report one of ``BINDING_CODES`` (its message is never read)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtocolError(Exception):
    pass


def _header(raw: bytes) -> tuple[dict[str, Any], bytes]:
    split = raw.find(b"\n")
    if split < 0 or split > MAX_HEADER_BYTES:
        raise ProtocolError("header")
    try:
        header = json.loads(raw[:split].decode("ascii"))
    except ValueError:
        raise ProtocolError("header") from None
    if not isinstance(header, dict) or set(header) != {"media_type", "pages", "languages"}:
        raise ProtocolError("header")
    pages, languages = header["pages"], header["languages"]
    if (not isinstance(header["media_type"], str) or not isinstance(pages, list) or len(pages) > 100_000
            or any(isinstance(p, bool) or not isinstance(p, int) or p < 1 for p in pages)
            or not isinstance(languages, list) or len(languages) > 8
            or any(not isinstance(code, str) or len(code) > 35 for code in languages)):
        raise ProtocolError("header")
    return header, raw[split + 1:]


def _resolve(target: str) -> Callable[..., Any] | None:
    module_name, _, function_name = target.partition(":")
    try:
        function = getattr(importlib.import_module(module_name), function_name)
    except MemoryError:
        raise
    except Exception:  # an import failure never echoes the operator module's text
        return None
    return function if callable(function) else None


def call(target: str, raw: bytes) -> dict[str, Any]:
    header, payload = _header(raw)
    function = _resolve(target)
    if function is None:
        return {"ok": False, "code": BINDING_IMPORT_FAILED}
    try:
        result = function(payload, media_type=header["media_type"], pages=tuple(header["pages"]),
                          languages=tuple(header["languages"]))
    except MemoryError:
        raise
    except BindingRefusal as refusal:
        return {"ok": False, "code": refusal.code if refusal.code in BINDING_CODES else "ENGINE_FAILED"}
    except Exception:  # engine glue failures never echo engine messages
        return {"ok": False, "code": "ENGINE_FAILED"}
    if not isinstance(result, Mapping):
        return {"ok": False, "code": "ENGINE_OUTPUT_INVALID"}
    return {"ok": True, "result": dict(result)}


def reply(work: Callable[[], dict[str, Any]]) -> int:
    """Run ``work`` and write its canonical reply; MemoryError -> EXIT_BUDGET, protocol -> EXIT_PROTOCOL."""
    try:
        outcome = work()
    except MemoryError:
        return EXIT_BUDGET
    except ProtocolError:
        return EXIT_PROTOCOL
    try:
        data = json.dumps(outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except MemoryError:
        return EXIT_BUDGET
    except (ValueError, TypeError, RecursionError):  # not JSON: the binding broke the result contract
        data = b'{"code":"ENGINE_OUTPUT_INVALID","ok":false}'
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Limits first (before stdin is read or the operator module is imported); refuse to run without them."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or not enter_limits(args[:1]):
        return EXIT_LIMITS_REFUSED
    target = args[1]
    if not _TARGET.fullmatch(target):
        return EXIT_PROTOCOL
    return reply(lambda: call(target, sys.stdin.buffer.read()))


if __name__ == "__main__":
    # Run with -m this module is ``__main__``; an operator module that imports ``BindingRefusal`` by
    # its real name must get THIS module (and class), not a second copy.
    sys.modules.setdefault("pados.packs.ocr._binding_worker", sys.modules[__name__])
    sys.exit(main())
