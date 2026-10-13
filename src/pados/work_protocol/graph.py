# SPDX-License-Identifier: AGPL-3.0-only
"""Rebuildable work-map projection from task, assignments, results, and receipts."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.work_protocol.coverage import assert_coverage_matches_results, build_coverage
from pados.work_protocol.models import (
    ArtifactReference,
    CapabilityResult,
    CoverageMap,
    ExecutionReceiptRef,
    GoalRelationType,
    ResidualTask,
    ResourceKind,
    ResultCandidate,
    TaskEnvelope,
    WorkAssignment,
    WorkEdgeKind,
    WorkGraph,
    WorkGraphEdge,
    WorkGraphNode,
    WorkNodeKind,
)


class RuntimeLineageProjection(BaseModel):
    """Minimized authoritative references used to extend the rebuildable graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    goal_id: str
    project_ref: str
    classification: DataClassification
    residual_id: str | None = None
    parent_goal_id: str | None = None
    amendment_id: str | None = None
    authority_decision: Literal["AUTO_AMENDABLE", "REAPPROVAL_REQUIRED"] | None = None
    plan_id: str
    step_id: str
    sequence: int = Field(gt=0)
    step_status: str = Field(max_length=40)
    worker_id: str | None = None
    capability: str | None = None
    receipt_ref: ExecutionReceiptRef | None = None
    artifacts: tuple[ArtifactReference, ...] = Field(default=(), max_length=32)

    @field_validator(
        "task_id", "goal_id", "project_ref", "residual_id", "parent_goal_id",
        "amendment_id", "plan_id", "step_id", "worker_id", "capability",
    )
    @classmethod
    def references_are_bounded(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 160):
            raise ValueError("WORK_GRAPH_RUNTIME_REF_INVALID")
        return value


def project_work_graph(
    task: TaskEnvelope,
    results: tuple[CapabilityResult, ...],
    candidate: ResultCandidate | None = None,
    *,
    coverage: CoverageMap | None = None,
    assignments: Sequence[WorkAssignment] = (),
    residuals: Sequence[ResidualTask] = (),
) -> WorkGraph:
    if coverage is None:
        coverage = build_coverage(task, results)
    if coverage.task_id != task.task_id or coverage.task_envelope_hash != task.content_hash:
        raise ValueError("WORK_GRAPH_TASK_BINDING_MISMATCH")
    assert_coverage_matches_results(
        task, coverage, results, error_code="WORK_GRAPH_COVERAGE_RESULTS_MISMATCH"
    )
    nodes: dict[str, WorkGraphNode] = {}
    edges: list[WorkGraphEdge] = []

    def add_node(node: WorkGraphNode) -> None:
        existing = nodes.get(node.node_id)
        if existing is not None:
            if existing.kind is not node.kind or existing.label != node.label:
                raise ValueError("WORK_GRAPH_NODE_CONFLICT")
            metadata = dict(existing.metadata)
            for key, value in node.metadata.items():
                if key in metadata and metadata[key] != value:
                    raise ValueError("WORK_GRAPH_METADATA_CONFLICT")
                metadata[key] = value
            order = {"PENDING": 0, "READY": 1, "ASSIGNED": 2, "RUNNING": 3,
                     "REGISTERED": 2, "EXECUTED": 4, "PARTIAL": 4, "COMPLETE": 5,
                     "BLOCKED": 6, "REJECTED": 7, "ACTIVE": 2}
            status = node.status if order.get(node.status, 0) > order.get(existing.status, 0) else existing.status
            node = existing.model_copy(update={"status": status, "metadata": metadata})
        nodes[node.node_id] = node

    project_node_id = f"project:{task.project_ref}"
    add_node(
        WorkGraphNode(
            node_id=project_node_id,
            kind=WorkNodeKind.PROJECT,
            label=task.project_ref,
            status="ACTIVE",
            metadata={"classification": task.classification.value},
        )
    )
    add_node(
        WorkGraphNode(
            node_id=task.task_id,
            kind=WorkNodeKind.TASK,
            label=task.original_intent[:160],
            status=(
                "WAITING_ACCEPTANCE"
                if all(entry.status.value in {"COMPLETE", "REJECTED"} for entry in coverage.entries)
                else "IN_PROGRESS"
            ),
            metadata={"priority": task.priority.value, "scope_version_ref": task.scope_version_ref},
        )
    )
    edges.append(WorkGraphEdge(source_id=project_node_id, target_id=task.task_id, kind=WorkEdgeKind.CONTAINS))
    coverage_by_goal = {entry.goal_id: entry for entry in coverage.entries}
    for goal in task.normalized_goals:
        state = coverage_by_goal[goal.goal_id].status.value
        add_node(
            WorkGraphNode(
                node_id=goal.goal_id,
                kind=WorkNodeKind.GOAL,
                label=goal.title,
                status=state,
                metadata={"required_capabilities": ",".join(goal.required_capabilities)},
            )
        )
        edges.append(WorkGraphEdge(source_id=task.task_id, target_id=goal.goal_id, kind=WorkEdgeKind.CONTAINS))
        for dependency in goal.dependencies:
            edges.append(
                WorkGraphEdge(source_id=goal.goal_id, target_id=dependency, kind=WorkEdgeKind.DEPENDS_ON)
            )
    for relation in task.goal_relations:
        if relation.relation is GoalRelationType.DEPENDS_ON:
            kind = WorkEdgeKind.DEPENDS_ON
        elif relation.relation is GoalRelationType.BLOCKED_BY:
            kind = WorkEdgeKind.BLOCKED_BY
        elif relation.relation is GoalRelationType.RESIDUAL_OF:
            kind = WorkEdgeKind.RESIDUAL_OF
        elif relation.relation is GoalRelationType.PARENT_OF:
            kind = WorkEdgeKind.CONTAINS
            edges.append(WorkGraphEdge(source_id=relation.source_goal_id, target_id=relation.target_goal_id,
                                       kind=kind, reason=relation.relation.value))
            continue
        else:
            kind = WorkEdgeKind.DERIVED_FROM
        edges.append(
            WorkGraphEdge(
                source_id=relation.source_goal_id,
                target_id=relation.target_goal_id,
                kind=kind,
                reason=relation.relation.value,
            )
        )
    for assignment in assignments:
        if assignment.task_id != task.task_id or assignment.goal_id not in coverage_by_goal:
            raise ValueError("WORK_GRAPH_ASSIGNMENT_BINDING_MISMATCH")
        worker_kind = WorkNodeKind.HUMAN if assignment.worker_id.startswith("human:") else WorkNodeKind.AGENT
        worker_node_id = f"worker:{assignment.worker_id}"
        capability_node_id = f"capability:{assignment.capability}"
        add_node(WorkGraphNode(node_id=worker_node_id, kind=worker_kind, label=assignment.worker_id,
                               status=assignment.status.value, metadata={}))
        add_node(WorkGraphNode(node_id=capability_node_id, kind=WorkNodeKind.CAPABILITY,
                               label=assignment.capability, status="REGISTERED", metadata={}))
        edges.append(WorkGraphEdge(source_id=assignment.goal_id, target_id=worker_node_id,
                                   kind=WorkEdgeKind.ASSIGNED_TO, reason=assignment.reason))
        edges.append(WorkGraphEdge(source_id=worker_node_id, target_id=capability_node_id,
                                   kind=WorkEdgeKind.EXECUTED_BY))
    for result in results:
        if result.task_id != task.task_id or result.goal_id not in coverage_by_goal:
            raise ValueError("WORK_GRAPH_RESULT_BINDING_MISMATCH")
        worker_node_id = f"worker:{result.provenance.worker_id}"
        worker_kind = WorkNodeKind.HUMAN if result.provenance.worker_id.startswith("human:") else WorkNodeKind.AGENT
        add_node(WorkGraphNode(node_id=worker_node_id, kind=worker_kind,
                               label=result.provenance.worker_id, status="EXECUTED", metadata={}))
        capability_node_id = f"capability:{result.capability}"
        add_node(WorkGraphNode(node_id=capability_node_id, kind=WorkNodeKind.CAPABILITY,
                               label=result.capability, status="EXECUTED", metadata={}))
        resource_kind = _resource_node_kind(result.resource_kind)
        resource_node_id = f"resource:{result.resource_id}"
        add_node(WorkGraphNode(node_id=resource_node_id, kind=resource_kind,
                               label=result.provider or result.resource_id, status="EXECUTED", metadata={
                                   "resource_kind": result.resource_kind.value,
                               }))
        edges.append(WorkGraphEdge(source_id=result.goal_id, target_id=worker_node_id,
                                   kind=WorkEdgeKind.EXECUTED_BY))
        edges.append(WorkGraphEdge(source_id=worker_node_id, target_id=capability_node_id,
                                   kind=WorkEdgeKind.EXECUTED_BY))
        edges.append(WorkGraphEdge(source_id=capability_node_id, target_id=resource_node_id,
                                   kind=WorkEdgeKind.ASSIGNED_TO))
        receipt = result.execution_receipt_ref
        receipt_node_id = f"receipt:{receipt.receipt_id}"
        add_node(WorkGraphNode(node_id=receipt_node_id, kind=WorkNodeKind.RECEIPT,
                               label=receipt.receipt_id, status="RECORDED", metadata={
                                   "digest": receipt.digest,
                                   "protocol_version": receipt.protocol_version,
                                   "schema_version": receipt.schema_version,
                               }))
        edges.append(WorkGraphEdge(source_id=resource_node_id, target_id=receipt_node_id,
                                   kind=WorkEdgeKind.PRODUCED))
        for artifact in result.artifacts:
            if artifact.project_ref != task.project_ref:
                raise ValueError("WORK_GRAPH_ARTIFACT_PROJECT_MISMATCH")
            artifact_node_id = f"artifact:{artifact.artifact_id}"
            add_node(WorkGraphNode(node_id=artifact_node_id, kind=WorkNodeKind.ARTIFACT,
                                   label=artifact.artifact_id, status="PRODUCED", metadata={
                                       "digest": artifact.digest,
                                       "classification": artifact.classification.value,
                                   }))
            edges.append(WorkGraphEdge(source_id=capability_node_id, target_id=artifact_node_id,
                                       kind=WorkEdgeKind.PRODUCED))
    for residual in residuals:
        if residual.task_id != task.task_id:
            raise ValueError("WORK_GRAPH_RESIDUAL_BINDING_MISMATCH")
        residual_node_id = residual.residual_task_id
        residual_status = (
            "COMPLETE"
            if all(coverage_by_goal[residual_goal.goal_id].status.value == "COMPLETE"
                   for residual_goal in residual.goals)
            else "READY" if residual.automatic_routing_allowed else "BLOCKED"
        )
        add_node(WorkGraphNode(node_id=residual_node_id, kind=WorkNodeKind.TASK,
                               label=f"Residual work ({len(residual.goals)} goals)", status=residual_status,
                               metadata={"automatic_routing_allowed": residual.automatic_routing_allowed}))
        edges.append(WorkGraphEdge(source_id=residual_node_id, target_id=task.task_id,
                                   kind=WorkEdgeKind.RESIDUAL_OF))
        for residual_goal in residual.goals:
            edges.append(WorkGraphEdge(source_id=residual_node_id, target_id=residual_goal.goal_id,
                                       kind=WorkEdgeKind.CONTAINS))
    if candidate is not None:
        if (
            candidate.task_id != task.task_id
            or candidate.project_ref != task.project_ref
            or candidate.classification is not task.classification
            or candidate.scope_version_ref != task.scope_version_ref
        ):
            raise ValueError("WORK_GRAPH_CANDIDATE_BINDING_MISMATCH")
        decision_node_id = f"decision:{candidate.candidate_id}"
        add_node(WorkGraphNode(node_id=decision_node_id, kind=WorkNodeKind.DECISION,
                               label="Result candidate", status=candidate.semantic_acceptance,
                               metadata={"verification_status": candidate.verification_status.value,
                                         "conflict_count": len(candidate.conflicts)}))
        edges.append(WorkGraphEdge(source_id=task.task_id, target_id=decision_node_id,
                                   kind=WorkEdgeKind.DERIVED_FROM))
        result_receipt = candidate.result_receipt_ref
        if result_receipt is not None:
            receipt_node_id = f"receipt:{result_receipt.receipt_id}"
            add_node(WorkGraphNode(node_id=receipt_node_id, kind=WorkNodeKind.RECEIPT,
                                   label=result_receipt.receipt_id, status="RECORDED", metadata={
                                       "digest": result_receipt.digest,
                                       "protocol_version": result_receipt.protocol_version,
                                       "schema_version": result_receipt.schema_version,
                                   }))
            edges.append(WorkGraphEdge(source_id=decision_node_id, target_id=receipt_node_id,
                                       kind=WorkEdgeKind.PRODUCED))
    source_hashes = [task.content_hash, coverage.content_hash]
    source_hashes.extend(result.content_hash for result in results)
    source_hashes.extend(assignment.content_hash for assignment in assignments)
    source_hashes.extend(residual.content_hash for residual in residuals)
    if candidate is not None:
        source_hashes.append(candidate.content_hash)
    return WorkGraph(
        protocol_version="1.0",
        schema_version=WorkGraph.SCHEMA,
        projection_id=f"work-graph:{canonical_hash(sorted(source_hashes))[:24]}",
        source_hashes=tuple(source_hashes),
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(sorted(edges, key=lambda edge: (edge.source_id, edge.kind.value, edge.target_id))),
    )


def project_p6_10_lineage(
    graph: WorkGraph,
    lineage: Sequence[RuntimeLineageProjection],
) -> WorkGraph:
    """Add rebuildable TaskLoop identities without introducing graph authority."""
    nodes = {node.node_id: node for node in graph.nodes}
    edges = {(edge.source_id, edge.target_id, edge.kind, edge.reason): edge for edge in graph.edges}
    hashes = set(graph.source_hashes)

    def add_node(node: WorkGraphNode) -> None:
        existing = nodes.get(node.node_id)
        if existing is not None and (existing.kind is not node.kind or existing.label != node.label):
            raise ValueError("WORK_GRAPH_RUNTIME_NODE_CONFLICT")
        nodes[node.node_id] = node if existing is None else existing.model_copy(
            update={"status": node.status, "metadata": {**existing.metadata, **node.metadata}}
        )

    def add_edge(source_id: str, target_id: str, kind: WorkEdgeKind) -> None:
        edge = WorkGraphEdge(source_id=source_id, target_id=target_id, kind=kind)
        edges[(edge.source_id, edge.target_id, edge.kind, edge.reason)] = edge

    for item in lineage:
        if item.task_id not in nodes or item.goal_id not in nodes:
            raise ValueError("WORK_GRAPH_RUNTIME_TASK_OR_GOAL_MISSING")
        project_node_id = f"project:{item.project_ref}"
        if project_node_id not in nodes:
            raise ValueError("WORK_GRAPH_RUNTIME_PROJECT_MISSING")
        if str(nodes[project_node_id].metadata.get("classification", "")).upper() != item.classification.value:
            raise ValueError("WORK_GRAPH_RUNTIME_CLASSIFICATION_MISMATCH")
        step_node_id = f"plan-step:{item.step_id}"
        add_node(
            WorkGraphNode(
                node_id=step_node_id,
                kind=WorkNodeKind.PLAN_STEP,
                label=f"P6.10 step {item.sequence}",
                status=item.step_status,
                metadata={"plan_ref": item.plan_id, "step_ref": item.step_id},
            )
        )
        add_edge(item.goal_id, step_node_id, WorkEdgeKind.CONTAINS)
        if item.residual_id is not None:
            existing_residual = nodes.get(item.residual_id)
            if existing_residual is None:
                add_node(
                    WorkGraphNode(
                        node_id=item.residual_id,
                        kind=WorkNodeKind.TASK,
                        label=f"Residual {item.goal_id}",
                        status=item.step_status,
                        metadata={"goal_ref": item.goal_id},
                    )
                )
            elif existing_residual.kind is not WorkNodeKind.TASK:
                raise ValueError("WORK_GRAPH_RUNTIME_RESIDUAL_KIND_MISMATCH")
            add_edge(item.residual_id, item.goal_id, WorkEdgeKind.CONTAINS)
            if item.parent_goal_id is not None:
                add_edge(item.residual_id, item.parent_goal_id, WorkEdgeKind.RESIDUAL_OF)
        if item.amendment_id is not None:
            amendment_node_id = f"amendment:{item.amendment_id}"
            add_node(
                WorkGraphNode(
                    node_id=amendment_node_id,
                    kind=WorkNodeKind.AMENDMENT,
                    label="Residual plan amendment",
                    status=item.authority_decision or "AUTO_AMENDABLE",
                    metadata={"plan_ref": item.plan_id, "step_ref": item.step_id},
                )
            )
            add_edge(item.goal_id, amendment_node_id, WorkEdgeKind.DERIVED_FROM)
            if item.parent_goal_id is not None:
                add_edge(amendment_node_id, item.parent_goal_id, WorkEdgeKind.RESIDUAL_OF)
            add_edge(amendment_node_id, step_node_id, WorkEdgeKind.PRODUCED)
        if item.worker_id is not None:
            worker_node_id = f"worker:{item.worker_id}"
            add_node(
                WorkGraphNode(
                    node_id=worker_node_id,
                    kind=WorkNodeKind.HUMAN if item.worker_id.startswith("human:") else WorkNodeKind.AGENT,
                    label=item.worker_id,
                    status="EXECUTED" if item.receipt_ref is not None else "ASSIGNED",
                    metadata={},
                )
            )
            add_edge(step_node_id, worker_node_id, WorkEdgeKind.ASSIGNED_TO)
            if item.capability is not None:
                capability_node_id = f"capability:{item.capability}"
                add_node(
                    WorkGraphNode(
                        node_id=capability_node_id,
                        kind=WorkNodeKind.CAPABILITY,
                        label=item.capability,
                        status="EXECUTED" if item.receipt_ref is not None else "REGISTERED",
                        metadata={},
                    )
                )
                add_edge(worker_node_id, capability_node_id, WorkEdgeKind.EXECUTED_BY)
                if item.receipt_ref is not None:
                    receipt_node_id = f"receipt:{item.receipt_ref.receipt_id}"
                    add_node(
                        WorkGraphNode(
                            node_id=receipt_node_id,
                            kind=WorkNodeKind.RECEIPT,
                            label=item.receipt_ref.receipt_id,
                            status="RECORDED",
                            metadata={"digest": item.receipt_ref.digest},
                        )
                    )
                    add_edge(capability_node_id, receipt_node_id, WorkEdgeKind.PRODUCED)
                    for artifact in item.artifacts:
                        artifact_node_id = f"artifact:{artifact.artifact_id}"
                        add_node(
                            WorkGraphNode(
                                node_id=artifact_node_id,
                                kind=WorkNodeKind.ARTIFACT,
                                label=artifact.artifact_id,
                                status="PRODUCED",
                                metadata={
                                    "digest": artifact.digest,
                                    "classification": artifact.classification.value,
                                },
                            )
                        )
                        add_edge(receipt_node_id, artifact_node_id, WorkEdgeKind.PRODUCED)
        hashes.add(canonical_hash(item.model_dump(mode="json")))

    source_hashes = tuple(sorted(hashes))
    return WorkGraph(
        protocol_version="1.0",
        schema_version=WorkGraph.SCHEMA,
        projection_id=f"work-graph:{canonical_hash(source_hashes)[:24]}",
        source_hashes=source_hashes,
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(sorted(edges.values(), key=lambda edge: (edge.source_id, edge.kind.value, edge.target_id))),
    )


def _resource_node_kind(resource_kind: ResourceKind) -> WorkNodeKind:
    if resource_kind in {ResourceKind.LOCAL_MODEL, ResourceKind.CLOUD_MODEL, ResourceKind.VISION_ENGINE}:
        return WorkNodeKind.MODEL
    if resource_kind is ResourceKind.HUMAN:
        return WorkNodeKind.HUMAN
    if resource_kind is ResourceKind.COMPUTE_NODE:
        return WorkNodeKind.AGENT
    return WorkNodeKind.TOOL


class AcceptanceLineageProjection(BaseModel):
    """Minimized references to the governed acceptance records of one ResultCandidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    project_ref: str
    candidate_id: str
    candidate_digest: str
    verification_status: Literal["PASSED", "FAILED"]
    verification_digest: str
    approval_ref: str | None = None
    decision: Literal["APPROVE", "REJECT"] | None = None
    decided_by: str | None = None
    proof_ref: str | None = None
    next_action: str | None = None
    result_receipt_id: str | None = None
    result_receipt_digest: str | None = None
    commit_status: str | None = None
    committed_scope_version: int | None = None

    @property
    def acceptance_status(self) -> str:
        if self.verification_status == "FAILED":
            return "VERIFICATION_FAILED"
        if self.decision is None:
            return "WAITING_APPROVAL"
        if self.decision == "REJECT":
            return "REJECTED"
        if self.commit_status == "COMMITTED":
            return "ACCEPTED"
        return "APPROVED_NOT_COMMITTED"


def project_acceptance_lineage(graph: WorkGraph, acceptance: AcceptanceLineageProjection) -> WorkGraph:
    """Extend the projection with Verification -> Approval -> ResultReceipt -> accepted state.

    Every node is derived from persisted acceptance records and the proof-database commit;
    the graph never decides, approves or commits anything.
    """
    nodes = {node.node_id: node for node in graph.nodes}
    edges = {(edge.source_id, edge.target_id, edge.kind, edge.reason): edge for edge in graph.edges}
    if acceptance.task_id not in nodes:
        raise ValueError("WORK_GRAPH_ACCEPTANCE_TASK_MISSING")

    def put(node: WorkGraphNode) -> None:
        existing = nodes.get(node.node_id)
        if existing is not None and existing.kind is not node.kind:
            raise ValueError("WORK_GRAPH_ACCEPTANCE_NODE_CONFLICT")
        nodes[node.node_id] = node if existing is None else existing.model_copy(
            update={"status": node.status, "metadata": {**existing.metadata, **node.metadata}}
        )

    def link(source_id: str, target_id: str, kind: WorkEdgeKind, reason: str | None = None) -> None:
        edge = WorkGraphEdge(source_id=source_id, target_id=target_id, kind=kind, reason=reason)
        edges[(edge.source_id, edge.target_id, edge.kind, edge.reason)] = edge

    status = acceptance.acceptance_status
    candidate_node = f"decision:{acceptance.candidate_id}"
    put(WorkGraphNode(node_id=candidate_node, kind=WorkNodeKind.DECISION, label="Result candidate",
                      status=status, metadata={"candidate_digest": acceptance.candidate_digest}))
    link(acceptance.task_id, candidate_node, WorkEdgeKind.DERIVED_FROM)
    verification_node = f"verification:{acceptance.candidate_id}"
    put(WorkGraphNode(node_id=verification_node, kind=WorkNodeKind.DECISION, label="Verification",
                      status=acceptance.verification_status,
                      metadata={"verification_digest": acceptance.verification_digest,
                                "semantic_authority": False}))
    link(candidate_node, verification_node, WorkEdgeKind.VERIFIED_BY)
    if acceptance.verification_status == "PASSED":
        approval_node = f"approval:{acceptance.candidate_id}"
        approval_status = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}.get(
            acceptance.decision or "", "WAITING_APPROVAL"
        )
        put(WorkGraphNode(node_id=approval_node, kind=WorkNodeKind.DECISION, label="Human approval",
                          status=approval_status,
                          metadata={"approval_ref": acceptance.approval_ref, "proof_ref": acceptance.proof_ref,
                                    "step_up": "WEBAUTHN_USER_VERIFICATION" if acceptance.proof_ref else None,
                                    "next_action": acceptance.next_action}))
        link(verification_node, approval_node, WorkEdgeKind.PRODUCED)
        if acceptance.decided_by is not None:
            human_node = f"worker:human:{acceptance.decided_by}"
            put(WorkGraphNode(node_id=human_node, kind=WorkNodeKind.HUMAN, label=acceptance.decided_by[:160],
                              status="DECIDED", metadata={}))
            link(approval_node, human_node, WorkEdgeKind.ASSIGNED_TO)
        if acceptance.result_receipt_id is not None:
            receipt_node = f"receipt:{acceptance.result_receipt_id}"
            put(WorkGraphNode(node_id=receipt_node, kind=WorkNodeKind.RECEIPT, label="ResultReceipt",
                              status="RECORDED", metadata={"digest": acceptance.result_receipt_digest,
                                                           "commit_status": acceptance.commit_status}))
            link(approval_node, receipt_node, WorkEdgeKind.PRODUCED)
            if acceptance.commit_status == "COMMITTED" and acceptance.committed_scope_version is not None:
                state_node = f"semantic:{acceptance.project_ref}:{acceptance.committed_scope_version}"
                put(WorkGraphNode(node_id=state_node, kind=WorkNodeKind.DECISION,
                                  label=f"Accepted result (ScopeVersion {acceptance.committed_scope_version})",
                                  status="ACCEPTED",
                                  metadata={"scope_version": acceptance.committed_scope_version,
                                            "authority": "STATE_PROOF_CAS"}))
                link(receipt_node, state_node, WorkEdgeKind.PRODUCED)
    task = nodes[acceptance.task_id]
    nodes[acceptance.task_id] = task.model_copy(update={"status": status})
    hashes = set(graph.source_hashes)
    hashes.add(canonical_hash(acceptance.model_dump(mode="json")))
    source_hashes = tuple(sorted(hashes))
    return WorkGraph(
        protocol_version="1.0",
        schema_version=WorkGraph.SCHEMA,
        projection_id=f"work-graph:{canonical_hash(source_hashes)[:24]}",
        source_hashes=source_hashes,
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(sorted(edges.values(), key=lambda edge: (edge.source_id, edge.kind.value, edge.target_id))),
    )
