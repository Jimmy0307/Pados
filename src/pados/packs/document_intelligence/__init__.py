# SPDX-License-Identifier: AGPL-3.0-only
"""Document Intelligence Capability Packs (additive; never Core, never authority).

Real local engines for the retrieval capabilities that were contract-only: dense passage retrieval
(``PASSAGE_RETRIEVAL_DENSE``), deterministic reciprocal-rank fusion (``PASSAGE_RANK_FUSION``), cross-encoder
reranking in SHADOW or ACTIVE mode (``PASSAGE_RERANK_CROSS_ENCODER``) and the deterministic hand-off from a
ranked document to a Structured Problem Solving evidence pack (``PASSAGE_EVIDENCE_PACK``).

Every capability is a Capability Pack V1 manifest registered through the existing registry and run only by the
existing executor; outputs are candidates (``semantic_authority = NONE``). The model engines are optional: they
load only from an operator directory whose bytes match an audited pin (``model_pin``) and are otherwise
unavailable (ENGINE NOT INSTALLED), never a silent fallback. Nothing here downloads a model.
"""
