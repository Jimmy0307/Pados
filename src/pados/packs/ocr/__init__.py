# SPDX-License-Identifier: AGPL-3.0-only
"""OCR / Document Perception Capability Pack V1 (additive; never Core, never authority).

Provider-agnostic stages - preprocess, recognize, layout, tables, figures, structure - each a separate
replaceable capability (``descriptors``) with adapters implementing the shared
``pados.capability.adapter.CapabilityAdapter`` protocol. Outputs are ``ocr.document/v1`` candidates
that reference content-addressed artifacts (``semantic_authority = NONE``); acceptance happens only
through the existing receipt -> validation -> CAS path. No OCR engine is bundled or installed:
third-party engines are contract adapters that stay unavailable until an operator configures them.
"""
