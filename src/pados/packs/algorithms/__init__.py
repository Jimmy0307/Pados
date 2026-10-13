# SPDX-License-Identifier: AGPL-3.0-only
"""Algorithm Capability Pack V1: real algorithms instead of an LLM pretending to be one.

Pure-Python, deterministic reference implementations exposed as capability adapters (run only through
``pados.capability.adapter.run_bound_stage``): DRSA + DOMLEM rule induction (ROUGH_SET_RULE_LEARNING),
fuzzy cognitive map inference (FUZZY_COGNITIVE_MAP) and DEMATEL (MULTI_CRITERIA_DECISION_DEMATEL), plus
contract-only DANP, Choquet integral and ROV. Results are canonical, content-addressed artifacts with a
reproducibility record and DETERMINISTIC_REPLAY verification; they are candidates
(``semantic_authority = NONE``). Explanations are separate, labelled artifacts that can never replace a
result. Nothing here commits state, writes audit or Memory, or selects model providers.
"""
from __future__ import annotations

from pados.packs.algorithms.adapters import AlgorithmAdapter
from pados.packs.algorithms.contracts import AlgorithmError, Dataset, load_result
from pados.packs.algorithms.registry import ALGORITHMS, AlgorithmSpec, adapter_descriptor
from pados.packs.algorithms.replay import ExpectedStage, ReplayVerification, verify_replay

__all__ = [
    "ALGORITHMS",
    "AlgorithmAdapter",
    "AlgorithmError",
    "AlgorithmSpec",
    "Dataset",
    "ExpectedStage",
    "ReplayVerification",
    "adapter_descriptor",
    "load_result",
    "verify_replay",
]
