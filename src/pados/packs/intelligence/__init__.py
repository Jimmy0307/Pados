# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic Multi-Capability Intelligence Pipeline V1 (additive pack; never Core, never authority).

Phase G proves Thin Core + Capability Fabric end to end with ONE offline, deterministic pipeline whose
every stage is a separately registered, authorized and policy-evaluated capability executed by the
EXISTING fabric planner and executor (``pados.capability.planner`` / ``pados.capability.executor``):

    document -> DOCUMENT_PREPROCESS -> DOCUMENT_OCR (text layer) -> DOCUMENT_LAYOUT_ANALYSIS
             -> TABLE_EXTRACTION -> DOCUMENT_STRUCTURE_PARSE -> STRUCTURED_EXTRACTION -> ROUGH_SET_RULE_LEARNING
             -> RESULT_EXPLANATION_TEMPLATE, and DOCUMENT_STRUCTURE_PARSE -> PASSAGE_RETRIEVAL

This package holds only data and operator wiring: the decomposition templates (``template``), the
operator registration helper with its output validators (``wiring``), the pipeline report capability
plus its evidence builder (``report``) and the all-or-nothing provenance registrar the builder requires
(``provenance``). It adds no orchestrator, runner, router or scheduler, calls no
adapter directly, and never writes CAS, ScopeVersion, semantic state, Memory or audit. Every output is a
candidate (``semantic_authority = NONE``); the report is evidence, never acceptance.
"""
