# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit PADOS version model (see docs/product/VERSIONING_POLICY.md).

The product version is independent of internal milestone names (P6.x) and of
the contract/protocol/schema versions below, each of which evolves on its own.
"""
from __future__ import annotations

from typing import Any

PRODUCT_VERSION = "0.1.0a1"
RELEASE_CHANNEL = "alpha"


def version_report() -> dict[str, Any]:
    from pados.compute_node.manifest import NODE_PROTOCOL_VERSION, NODE_RUNTIME_VERSION
    from pados.compute_node.receipts import NODE_RECEIPT_SCHEMA
    from pados.config import OPERATIONAL_REVISION_DEFAULT, PROOF_REVISION_HEAD
    from pados.enterprise.manifest import MANIFEST_SCHEMA_VERSION
    from pados.enterprise.retrieval import RETRIEVAL_CONTRACT_VERSION

    return {
        "product": {"version": PRODUCT_VERSION, "channel": RELEASE_CHANNEL},
        "protocols": {"compute_node": NODE_PROTOCOL_VERSION, "enterprise_retrieval": RETRIEVAL_CONTRACT_VERSION},
        "schemas": {"capability_manifest": MANIFEST_SCHEMA_VERSION, "node_execution_receipt": NODE_RECEIPT_SCHEMA,
                    "execution_receipt": "1"},
        "runtimes": {"compute_node_daemon": NODE_RUNTIME_VERSION},
        "database": {"operational_revision": OPERATIONAL_REVISION_DEFAULT, "proof_revision": PROOF_REVISION_HEAD},
    }
