# SPDX-License-Identifier: AGPL-3.0-only
"""File persistence for the operator ``ModelCatalog`` document (the catalog stays the one model registry).

Not a new database, registry or authority: this stores exactly ``ModelCatalog.to_document()`` with the existing
operational-store helpers (``atomic_write_json`` and ``store_lock``, as used by the machine inventory and the
connectivity stores) and restores it with ``ModelCatalog.from_document``, which re-validates every measured profile
(its ``profile_id`` is recomputed from content). Persisting a profile keeps it REGISTERED only: the document has no
authorization, routability or lifecycle-promotion field, and loading never authorizes anything.
"""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

from pados.connectivity.storage import atomic_write_json, store_lock
from pados.resources.models import ModelCatalog

MAX_CATALOG_BYTES = 4 * 1024 * 1024


class CatalogStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelCatalogStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> ModelCatalog:
        """The persisted catalog; a missing file is an empty catalog, anything unreadable fails closed."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return ModelCatalog()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
            raise CatalogStoreError("MODEL_CATALOG_STORE_INVALID")
        try:
            document = json.loads(self.path.read_bytes())
            if not isinstance(document, dict):
                raise ValueError("MODEL_CATALOG_DOCUMENT_INVALID")
            return ModelCatalog.from_document(document)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CatalogStoreError("MODEL_CATALOG_STORE_INVALID") from exc

    def save(self, catalog: ModelCatalog) -> str:
        with store_lock(self.path):
            atomic_write_json(self.path, catalog.to_document())
        return catalog.catalog_digest

    def update(self, change: Callable[[ModelCatalog], ModelCatalog]) -> ModelCatalog:
        """Reload, apply ``change`` and persist as one locked transaction."""
        with store_lock(self.path):
            updated = change(self.load())
            atomic_write_json(self.path, updated.to_document())
            return updated
