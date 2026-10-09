# SPDX-License-Identifier: AGPL-3.0-only
"""Memory write-path content checks.

Memory holds bounded, reviewed knowledge. Secret-like content and raw terminal or
session output (VT/ANSI escape sequences, C0/C1 control characters) are refused at
the memory candidate and run-event write paths: terminal data is session data,
never PADOS memory.
"""
import json
import re
from typing import Any

from pados.execution.context import redact
from pados.retrieval.indexer import is_secret_suspected

MAX_PAYLOAD_BYTES = 32_000
MAX_SUMMARY_CHARS = 8_000

CREDENTIAL_URL_PATTERN = re.compile(r"://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)
# C0 controls except tab/newline/carriage return, DEL, and C1 controls (incl. 8-bit CSI).
TERMINAL_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def contains_secret(value: str) -> bool:
    return is_secret_suspected(value) or CREDENTIAL_URL_PATTERN.search(value) is not None


def assert_no_secret(value: str, *, field_name: str) -> None:
    if contains_secret(value):
        raise ValueError(f"secret-like content rejected in {field_name}")


def contains_terminal_output(value: str) -> bool:
    return TERMINAL_CONTROL_PATTERN.search(value) is not None


def assert_memory_content(value: str, *, field_name: str) -> None:
    """Memory writes: shared secret check, provider-token shapes, and no raw terminal output."""
    assert_no_secret(value, field_name=field_name)
    if redact(value) != value:  # execution-context detector: ghp_/github_pat_/xox*/AKIA/bearer shapes
        raise ValueError(f"secret-like content rejected in {field_name}")
    if contains_terminal_output(value):
        raise ValueError(f"raw terminal output rejected in {field_name}")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (str(key), *_strings(item))]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def validate_summary(summary: str) -> None:
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError("content_summary exceeds maximum length")
    assert_memory_content(summary, field_name="content_summary")


def validate_payload(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload_json exceeds maximum size")
    assert_no_secret(encoded, field_name="payload_json")
    for text in _strings(payload):  # json.dumps escapes control characters, so check raw values
        assert_memory_content(text, field_name="payload_json")
