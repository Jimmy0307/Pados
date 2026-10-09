"""Test harness: adapters run ONLY through bind_stage / run_bound_stage under a real PADOS grant."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pados.capability.adapter import (
    CapabilityAdapter,
    StageBinding,
    StageOutput,
    StageRequest,
    bind_stage,
    run_bound_stage,
)
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.model import CapabilityDescriptor
from pados.capability.policy import AuthorizedScope, PolicyContext, PolicyDecision, authorize_scope, evaluate
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms.contracts import DATASET_KIND, DATASET_SCHEMA, JSON_MEDIA

PROJECT = "proj-algo"
LEVELS = ["bad", "medium", "good"]


def contract(capability_id: str) -> dict[str, Any]:
    return {"work_contract_id": "wc-algo", "project_id": PROJECT,
            "required_capabilities": [f"CAPABILITY:{capability_id}"]}


def grant(*capability_ids: str) -> dict[str, Any]:
    return {"work_contract_id": "wc-algo", "project_id": PROJECT,
            "capabilities": [f"CAPABILITY:{cid}" for cid in capability_ids]}


def policy(capability: CapabilityDescriptor, adapter: CapabilityAdapter, *, stage_id: str = "s1",
           classification: DataClassification = DataClassification.CONFIDENTIAL,
           granted: Sequence[str] | None = None) -> tuple[PolicyContext, PolicyDecision, AuthorizedScope]:
    scope = authorize_scope(contract(capability.capability_id),
                            grant(*(granted if granted is not None else [capability.capability_id])))
    ctx = PolicyContext(scope=scope, project_id=PROJECT, stage_id=stage_id, classification=classification,
                        network_policy=NetworkRequirement.LOCAL_ONLY)
    return ctx, evaluate(capability, adapter.descriptor, ctx, adapter_available=adapter.available), scope


def run_bound(store: ObjectStoreArtifacts, capability: CapabilityDescriptor, adapter: CapabilityAdapter,
              inputs: Sequence[ArtifactRef], parameters: Mapping[str, Any] | None = None, *,
              stage_id: str = "s1", classification: DataClassification = DataClassification.CONFIDENTIAL,
              seed: int | None = None, router_selection_hash: str | None = None) -> tuple[StageBinding, StageOutput]:
    """The executor's view: the binding it built (trusted) and the output it observed."""
    ctx, decision, scope = policy(capability, adapter, stage_id=stage_id, classification=classification)
    request = StageRequest(stage_id, capability.capability_id, tuple(inputs), dict(parameters or {}),
                           classification, seed=seed, router_selection_hash=router_selection_hash)
    binding = bind_stage(capability, adapter.descriptor, ctx, decision, request,
                         permitted_inputs=frozenset(ref.sha256 for ref in inputs))
    return binding, run_bound_stage(binding, adapter, store, revalidate=lambda: scope)


def run_stage(store: ObjectStoreArtifacts, capability: CapabilityDescriptor, adapter: CapabilityAdapter,
              inputs: Sequence[ArtifactRef], parameters: Mapping[str, Any] | None = None, **kwargs: Any) -> StageOutput:
    return run_bound(store, capability, adapter, inputs, parameters, **kwargs)[1]


def exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and everything reachable through ``__cause__`` / ``__context__``."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        item = pending.pop()
        if item is None or any(item is known for known in seen):
            continue
        seen.append(item)
        pending += [item.__cause__, item.__context__]
    return seen


def put_json(store: ObjectStoreArtifacts, document: Any, *, kind: str = DATASET_KIND, indent: int | None = None) -> ArtifactRef:
    return store.put(json.dumps(document, indent=indent).encode("utf-8"), media_type=JSON_MEDIA, kind=kind)


def stored_objects(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


def students() -> dict[str, Any]:
    """Students example (Slowinski, Greco & Matarazzo, 'Rough set based decision support', tutorial in
    Burke & Kendall (eds.), Search Methodologies, 2005): Mathematics, Physics, Literature on
    bad < medium < good, comprehensive evaluation as decision class."""
    columns = [{"name": name, "type": "ORDINAL", "criterion": "GAIN", "domain": LEVELS}
               for name in ("M", "P", "L", "Overall")]
    rows = [["good", "medium", "bad", "bad"], ["medium", "medium", "bad", "medium"],
            ["medium", "medium", "medium", "medium"], ["good", "good", "medium", "good"],
            ["good", "medium", "good", "good"], ["good", "good", "good", "good"],
            ["bad", "bad", "bad", "bad"], ["bad", "bad", "medium", "bad"]]
    return {"schema": DATASET_SCHEMA, "columns": columns, "decision": "Overall",
            "object_ids": [f"S{i}" for i in range(1, 9)], "rows": rows}


def matrix(names: Sequence[str], rows: Sequence[Sequence[float]], **column: Any) -> dict[str, Any]:
    return {"schema": DATASET_SCHEMA, "columns": [{"name": n, "type": "NUMERIC", **column} for n in names],
            "object_ids": list(names), "rows": [list(r) for r in rows]}
