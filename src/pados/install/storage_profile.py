# SPDX-License-Identifier: AGPL-3.0-only
"""Installation storage modes -> ``StorageBudget`` (the one mapping; the integrator reconciles it here).

The profile is a plain JSON document of ``StorageBudget`` fields, so configuration loading (owned by
``pados.lifecycle.storage_flexibility``) can read it without this module. Every mode keeps SMALL AUTHORITATIVE
STATE locally; model weights are always EXTERNAL_REFERENCE (runtime-managed, never copied into PADOS storage).

* ``LIGHTWEIGHT`` (default): small reserve, small rebuildable cache, large artifacts go by reference.
* ``STANDARD``: larger cache and residency limit for a workstation.
* ``FULL_LOCAL``: no residency limit (artifacts may be held locally), weights still by reference.
"""
from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.lifecycle.storage_flexibility import StorageBudget

PROFILE_SCHEMA = "pados.install.storage-profile.v1"
_GIB = 2**30
_MIB = 2**20


class StorageMode(StrEnum):
    LIGHTWEIGHT = "LIGHTWEIGHT"
    STANDARD = "STANDARD"
    FULL_LOCAL = "FULL_LOCAL"


def budget_for_mode(mode: StorageMode, *, min_free_bytes: int | None = None) -> StorageBudget:
    """The single storage-mode mapping. ``min_free_bytes`` overrides the mode's reserve."""
    table: dict[StorageMode, dict[str, Any]] = {
        StorageMode.LIGHTWEIGHT: {"min_free_bytes": 2 * _GIB, "cache_budget_bytes": 512 * _MIB,
                                  "max_local_residency_bytes": 64 * _MIB},
        StorageMode.STANDARD: {"min_free_bytes": 5 * _GIB, "cache_budget_bytes": 4 * _GIB,
                               "max_local_residency_bytes": 1 * _GIB},
        StorageMode.FULL_LOCAL: {"min_free_bytes": 10 * _GIB, "cache_budget_bytes": None,
                                 "max_local_residency_bytes": None},
    }
    fields = dict(table[mode])
    if min_free_bytes is not None:
        fields["min_free_bytes"] = min_free_bytes
    return StorageBudget(**fields)


def profile_document(mode: StorageMode, budget: StorageBudget) -> dict[str, Any]:
    return {
        "schema": PROFILE_SCHEMA,
        "mode": mode.value,
        "budget": budget.model_dump(mode="json"),
        "model_weights": "EXTERNAL_REFERENCE",
        "cache_tier": "EPHEMERAL",
        "authority": "NONE",
    }


def write_profile(path: Path, mode: StorageMode, *, min_free_bytes: int | None = None) -> dict[str, Any]:
    document = profile_document(mode, budget_for_mode(mode, min_free_bytes=min_free_bytes))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    if os.name != "nt":
        path.chmod(0o600)
    return document


def load_profile(path: Path) -> tuple[StorageMode, StorageBudget]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != PROFILE_SCHEMA:
        raise ValueError("STORAGE_PROFILE_SCHEMA_INVALID")
    return StorageMode(document["mode"]), StorageBudget.model_validate(document["budget"])
