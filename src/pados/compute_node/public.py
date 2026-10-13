# SPDX-License-Identifier: AGPL-3.0-only
"""Public translation boundary for Local Compute Node names.

The frozen Worker Router sees node-backed models as its internal local-model
worker class (``WorkerType.OLLAMA``). That is an INTERNAL compatibility
mapping. Public surfaces (docs, configs, Mission Control, doctor, receipts)
use capability/provider terms only; this module is the single place that
translates between the two.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pados.compute_node.provider import LOCAL_MODEL_WORKER, PROVIDER_ID


class PublicBackend(StrEnum):
    OLLAMA_BACKEND = "OLLAMA_BACKEND"
    OPENAI_COMPATIBLE_BACKEND = "OPENAI_COMPATIBLE_BACKEND"
    LLAMA_CPP_BACKEND = "LLAMA_CPP_BACKEND"
    MLX_BACKEND = "MLX_BACKEND"
    DETERMINISTIC_BACKEND = "DETERMINISTIC_BACKEND"


BACKEND_KIND_TO_PUBLIC = {
    "ollama": PublicBackend.OLLAMA_BACKEND,
    "openai_compatible": PublicBackend.OPENAI_COMPATIBLE_BACKEND,
    "llama_cpp": PublicBackend.LLAMA_CPP_BACKEND,
    "llama_cpp_native": PublicBackend.LLAMA_CPP_BACKEND,
    "mlx_openai_compatible": PublicBackend.MLX_BACKEND,
    "deterministic": PublicBackend.DETERMINISTIC_BACKEND,
}
PUBLIC_EXECUTOR_KIND = "COMPUTE_NODE"
PUBLIC_CAPABILITY = "LOCAL_INFERENCE"


def public_backend(kind: str) -> PublicBackend:
    try:
        return BACKEND_KIND_TO_PUBLIC[kind]
    except KeyError:
        raise ValueError("BACKEND_KIND_UNKNOWN") from None


def public_worker_view(worker: Any) -> dict[str, Any]:
    """Translate a router candidate; node-backed workers never expose the internal class."""
    worker_id = str(worker.worker_id)
    if worker_id.startswith("lcn:") and worker.worker_type == LOCAL_MODEL_WORKER:
        _, node_id, model_id = worker_id.split(":", 2)
        return {"executor_kind": PUBLIC_EXECUTOR_KIND, "provider_id": PROVIDER_ID, "node_id": node_id,
                "model_id": model_id, "capability": PUBLIC_CAPABILITY}
    return {"executor_kind": "WORKER", "worker_id": worker_id, "worker_type": str(worker.worker_type.value)}
