# SPDX-License-Identifier: AGPL-3.0-only
"""Structured Extraction Capability Pack V1 (additive; never Core, never authority).

``STRUCTURED_EXTRACTION`` turns one table of an ``ocr.document/v1`` candidate into an
``algorithms.dataset`` artifact, with every column's type, preference direction and domain declared by
the stage parameters (never guessed from the text). The adapter is deterministic, pure Python and runs
only through ``pados.capability.adapter.run_bound_stage``; its output is a candidate
(``semantic_authority = NONE``) that the existing receipt -> validation -> CAS path may or may not accept.
"""
