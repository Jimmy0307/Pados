# SPDX-License-Identifier: AGPL-3.0-only
"""Governed research-document corpus foundation for PADOS."""

from .ingestion import (
    EMBEDDING_DIMENSION,
    MAX_PILOT_DOCUMENTS,
    IngestionState,
    StateTransition,
    cosine_similarity,
    deterministic_embedding,
    normalize_text,
    transition,
)

__all__ = [
    "EMBEDDING_DIMENSION",
    "MAX_PILOT_DOCUMENTS",
    "IngestionState",
    "StateTransition",
    "cosine_similarity",
    "deterministic_embedding",
    "normalize_text",
    "transition",
]
