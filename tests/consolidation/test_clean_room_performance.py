"""Performance contract for credential redaction (linear time on large context)."""
import time

import pytest

from pados.execution.context import redact

MIB = 1024 * 1024
SHAPES = {
    "word_run": "x" * MIB,
    "whitespace_run": "a" + " " * MIB + "b",
    "json_pairs": '{"k": "v", ' * (MIB // 11) + "}",
    "assignment_like": "word=" * (MIB // 5),
    "mixed_words": ("alpha beta gamma delta " * (MIB // 23)),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_redact_is_linear_on_large_inputs(shape):
    text = SHAPES[shape]
    started = time.perf_counter()
    assert redact(text) == text
    assert time.perf_counter() - started < 1.0, shape


def test_redact_still_detects_credentials_inside_large_inputs():
    marker = "Bearer " + "q" * 24
    for filler in ("x" * MIB, " " * MIB):
        started = time.perf_counter()
        assert redact(filler + marker + filler) != filler + marker + filler
        assert time.perf_counter() - started < 1.0
