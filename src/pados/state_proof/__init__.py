# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded P6.19B state-substrate proof seam."""

from typing import Any

__all__ = ["CognitiveProofProvider", "ObjectRef", "ObjectStore", "StateProofService"]


def __getattr__(name: str) -> Any:
    if name in {"ObjectRef", "ObjectStore"}:
        from pados.state_proof import object_store

        return getattr(object_store, name)
    if name in {"CognitiveProofProvider", "StateProofService"}:
        from pados.state_proof import service

        return getattr(service, name)
    raise AttributeError(name)
