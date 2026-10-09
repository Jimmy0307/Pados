# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
from typing import Any


def context_hash(packet: dict[str, Any]) -> str:
    payload = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.split()))


def compile_context(packet: dict[str, Any], *, max_tokens: int) -> dict[str, Any]:
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    payload = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    tokens = payload.split()
    truncated = len(tokens) > max_tokens
    if truncated:
        payload = " ".join(tokens[:max_tokens])
    compiled = {
        "content": payload,
        "estimated_tokens": min(_estimate_tokens(payload), max_tokens),
        "max_tokens": max_tokens,
        "truncated": truncated,
        "source_hash": context_hash(packet),
    }
    compiled["context_hash"] = context_hash(compiled)
    return compiled

