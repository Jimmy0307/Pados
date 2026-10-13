"""TEST-ONLY operator engine bindings for the OCR pack (never shipped, never imported by PADOS itself).

The tests make this directory importable for the isolated binding worker (``python -I``) by writing a
temporary ``.pth`` file into the virtual environment's site-packages, and remove it afterwards. Each
function has the binding signature ``function(payload, *, media_type, pages, languages)`` and runs
only inside ``pados.packs.ocr._binding_worker``, under its rlimits.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Any

from pados.packs.ocr._binding_worker import BindingRefusal

CONTENT_MARKER = "SECRET-DOCUMENT-TEXT-7f3a"


def _page(text: str, confidence: float = 0.9) -> dict[str, Any]:
    return {"schema_version": "ocr.engine-result/v1", "pages": [{
        "page_number": 1, "width": 64, "height": 16, "unit": "px",
        "lines": [{"text": text, "bbox": [2, 2, 60, 12], "confidence": confidence}]}]}


def recognize(payload: bytes, *, media_type: str, pages: tuple[int, ...], languages: tuple[str, ...]
              ) -> Mapping[str, Any]:
    """A well-behaved binding: proves it received the image and the request on stdin."""
    assert payload.startswith(b"\x89PNG") and media_type == "image/png" and pages == (1,)
    return _page(f"BOUND {len(payload)} {'+'.join(languages) or 'none'}")


def hog(payload: bytes, **_: Any) -> Mapping[str, Any]:
    """Allocates far past the memory limit (would succeed without it)."""
    blob = bytearray(1 << 30)
    return _page(f"allocated {len(blob)}")


def spin(payload: bytes, **_: Any) -> Mapping[str, Any]:
    while True:
        pass


def spawn(payload: bytes, **_: Any) -> Mapping[str, Any]:
    """Reports whether it could create a thread or a process (RLIMIT_NPROC = 0 refuses both)."""
    outcome = []
    try:
        threading.Thread(target=lambda: None).start()
        outcome.append("thread-created")
    except RuntimeError:
        outcome.append("thread-refused")
    try:
        child = os.fork()
    except OSError:
        outcome.append("fork-refused")
    else:
        if child == 0:
            os._exit(0)
        outcome.append("fork-created")
    return _page(" ".join(outcome))


def leaky(payload: bytes, **_: Any) -> Mapping[str, Any]:
    raise RuntimeError(CONTENT_MARKER)


def leaky_code(payload: bytes, **_: Any) -> Mapping[str, Any]:
    raise BindingRefusal(CONTENT_MARKER)


def timeout_code(payload: bytes, **_: Any) -> Mapping[str, Any]:
    raise BindingRefusal("ENGINE_TIMEOUT")


def not_a_mapping(payload: bytes, **_: Any) -> Any:
    return [CONTENT_MARKER]


def not_json(payload: bytes, **_: Any) -> Mapping[str, Any]:
    return {"pages": [b"raw-bytes"]}
