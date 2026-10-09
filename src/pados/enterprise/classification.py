# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise data classification.

PADOS stores ``classification`` as free text defaulting to ``internal``
(``projects``/``context_project_registry``); no accepted enumerated vocabulary
exists in PADOS or in the reference material reviewed for this pack (where
confidentiality ceilings were free strings). This module is the smallest ordered contract, and it parses the
existing lowercase PADOS value compatibly.
"""
from __future__ import annotations

from enum import StrEnum


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"

    @property
    def rank(self) -> int:
        return _ORDER[self]

    def permits(self, other: DataClassification) -> bool:
        """True when data at ``other`` may be handled under this ceiling."""
        return other.rank <= self.rank


_ORDER = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
}


def parse_classification(value: object) -> DataClassification:
    """Parse a classification; unknown or missing values fail closed."""
    if isinstance(value, DataClassification):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("CLASSIFICATION_REQUIRED")
    try:
        return DataClassification(value.strip().upper())
    except ValueError as exc:
        raise ValueError("CLASSIFICATION_UNKNOWN") from exc


def lowest(*values: DataClassification) -> DataClassification:
    """The most restrictive ceiling (lowest level) among several ceilings."""
    if not values:
        raise ValueError("CLASSIFICATION_REQUIRED")
    return min(values, key=lambda item: item.rank)
