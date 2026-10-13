# SPDX-License-Identifier: AGPL-3.0-only
"""Retrieval Capability Pack V1 (additive; never Core, never authority).

``PASSAGE_RETRIEVAL`` ranks the reading-order passages of an ``ocr.document/v1`` candidate for a query
taken from the stage parameters. The shipped adapter is deterministic lexical BM25 over a fixed tokenizer
with exact decimal arithmetic (``lexical``). Embedding retrieval and reranking are separate capabilities
whose adapters are contract-only descriptors (``engines``): no engine or model is bundled, installed or
downloaded, so they report ``available() == False`` (ENGINE NOT INSTALLED) and are never a silent fallback
for the lexical capability. Rankings are candidates (``semantic_authority = NONE``) that reference
passages by id and offset; they never carry document text.
"""
