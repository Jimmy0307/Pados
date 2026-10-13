# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed error type for Machine Connection.

A ``MachineError`` carries only an UPPER_SNAKE code (and optionally a machine
id). It never carries host credentials, a bootstrap password, raw probe output
or the input value that was refused, so its text is safe to log and display.
"""
from __future__ import annotations

import re

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


class MachineError(ValueError):
    def __init__(self, code: str, *, machine_id: str | None = None) -> None:
        if not _CODE.fullmatch(code):
            code = "MACHINE_ERROR"
        super().__init__(code)
        self.code = code
        self.machine_id = machine_id
