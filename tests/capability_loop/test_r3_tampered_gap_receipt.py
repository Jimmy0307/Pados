# SPDX-License-Identifier: AGPL-3.0-only
"""Receipt capability codes derived from a gap ref are always ASCII-valid (never raise).

A capability_gap_ref carrying Unicode alphanumerics (or other non-ASCII) must still yield a receipt-valid
ASCII capability code, so deriving a code for a refusal receipt can never raise an OperationReceipt
validation error.
"""
from __future__ import annotations

import pytest

from pados.capability_loop.learning import _gap_capability, _safe_capability_code
from pados.capability_loop.receipts import is_valid_receipt_code


@pytest.mark.parametrize("gap_ref", [
    "gap:ééé",       # Unicode alphanumerics
    "gap:café_edit",           # mixed ascii + unicode
    "gap:你好",             # CJK
    "gap:",                         # empty tail
    "gap:--",                       # only separators
    "gap:cad_edit",                 # normal
    "gap:OCR",                      # short
])
def test_derived_capability_code_is_always_receipt_valid(gap_ref: str) -> None:
    code = _gap_capability(gap_ref)
    assert is_valid_receipt_code(code)
    assert _safe_capability_code(gap_ref) == code  # the guard returns the same valid ASCII code


def test_normal_gap_is_uppercased_ascii() -> None:
    assert _gap_capability("gap:cad_edit") == "CAD_EDIT"
