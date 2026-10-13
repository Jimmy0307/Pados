"""Governed Work Contract -> LocalComputeNodeMaker -> Core validation/CAS (in-memory gateway)."""
import asyncio
import json

from node_support import PROJECT, resolver

from pados.compute_node.maker import BATCH_SCHEMA, LocalComputeNodeMaker
from pados.compute_node.provider import LocalInferenceProvider
from pados.compute_node.receipts import ReceiptLedger
from pados.product_loop.schemas import GoalRequest, ProductLoopStatus
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator

TARGET = "docs/NODE_BRIEF.md"


class Evidence:
    """Server-side context extension supplying classified evidence (enterprise contract shape)."""

    name = "enterprise_evidence"

    def __init__(self, classification):
        self.classification = classification

    def resolve(self, *, scope_id, goal):
        return {"semantic_authority": "NONE", "package": {"evidence": [
            {"source_id": "sha256:" + "a" * 64, "classification": self.classification,
             "text": "Synthetic handbook: inspect the coolant pump every 90 days."}]}}


def _loop(node, tmp_path, registration=None, classification="INTERNAL", maker_cls=LocalComputeNodeMaker):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    registry = node.registry(tmp_path, registration or node.registration())
    provider = LocalInferenceProvider(registry, resolver=resolver(), http=node.http, poll_interval=0.01)
    gateway = InMemoryP6_19BStateGateway(tmp_path / "objects")
    maker = maker_cls(workspace, provider, timeout_seconds=5)
    loop = ProductLoopService(maker=maker, validator=FileValidator(), state_gateway=gateway,
                              approved_workspace_root=workspace, context_extensions=(Evidence(classification),))
    return loop, gateway, workspace, registry


def _request(workspace, content="Summarize the evidence.", expected="Deterministic local completion", key="k"):
    return GoalRequest.model_validate({
        "goal": "node brief", "workspace_root": str(workspace), "scope_id": PROJECT, "idempotency_key": key,
        "changes": [{"path": TARGET, "content": content}],
        "acceptance_criteria": [{"criterion_id": "c", "check": "FILE_CONTAINS", "target_path": TARGET,
                                 "expected_text": expected}, {"criterion_id": "s", "check": "CHANGED_PATHS_SCOPED"}],
        "authorized": True})


def test_authorized_local_execution_commits_only_through_core_cas(node, tmp_path):
    loop, gateway, workspace, registry = _loop(node, tmp_path)
    report = asyncio.run(loop.execute(_request(workspace)))
    assert report.status == ProductLoopStatus.COMMITTED and gateway.versions[PROJECT] == 1
    receipt = report.final_result.execution_receipt
    assert receipt["provider_id"] == "PADOS_LOCAL_INFERENCE" and receipt["semantic_authority"] is False
    observation = report.final_result.validation_observations[0]
    assert observation["kind"] == "NODE_EXECUTION_RECEIPT" and observation["node_id"] == node.node_id
    node_receipt = ReceiptLedger(registry.state_dir).latest(node.node_id)
    assert node_receipt["execution_receipt_id"] == receipt["receipt_id"]
    assert node_receipt["run_id"] == report.run_id and node_receipt["work_package_id"] == report.contracts[0].work_contract_id


def test_unauthorized_node_fails_before_execution(node, tmp_path):
    loop, gateway, workspace, _ = _loop(node, tmp_path, node.registration(authorized=False))
    report = asyncio.run(loop.execute(_request(workspace)))
    assert report.status != ProductLoopStatus.COMMITTED and not gateway.commits
    assert report.final_result.summary == "NO_ELIGIBLE_LOCAL_NODE"
    assert report.final_result.failure_classification.value == "AUTHORITY_BLOCK"
    assert not (workspace / TARGET).exists() and node.runtime.jobs.load().running == 0
    assert not node.runtime.jobs._jobs  # nothing reached the node


def test_classification_policy_blocks_restricted_evidence_on_unapproved_node(node, tmp_path):
    loop, gateway, workspace, _ = _loop(node, tmp_path, classification="RESTRICTED")  # ceiling CONFIDENTIAL
    report = asyncio.run(loop.execute(_request(workspace)))
    assert report.final_result.failure_classification.value == "AUTHORITY_BLOCK" and not gateway.commits
    assert not node.runtime.jobs._jobs


def test_tampered_workspace_artifact_is_not_committed(node, tmp_path):
    class TamperingMaker(LocalComputeNodeMaker):
        async def make(self, contract, *, run_id, attempt):
            result = await super().make(contract, run_id=run_id, attempt=attempt)
            (self.approved_root / TARGET).write_text("tampered after verification\n")
            return result

    loop, gateway, workspace, _ = _loop(node, tmp_path, maker_cls=TamperingMaker)
    report = asyncio.run(loop.execute(_request(workspace)))
    assert report.status != ProductLoopStatus.COMMITTED and not gateway.commits


def test_bounded_batch_work_contract(node, tmp_path):
    loop, _gateway, workspace, _ = _loop(node, tmp_path)
    spec = json.dumps({"schema": BATCH_SCHEMA, "capability": "CLASSIFICATION", "items": [
        {"text": "pump leak", "labels": {"maintenance": ["pump"], "finance": ["price"]}},
        {"text": "price", "labels": {"maintenance": ["pump"], "finance": ["price"]}}]})
    request = _request(workspace, content=spec, expected='"index": 1', key="batch")
    request = request.model_copy(update={"changes": [request.changes[0].model_copy(update={"path": "batch/labels.jsonl"})],
                                         "acceptance_criteria": [request.acceptance_criteria[0].model_copy(
                                             update={"target_path": "batch/labels.jsonl"})]})
    report = asyncio.run(loop.execute(request))
    assert report.status == ProductLoopStatus.COMMITTED
    lines = (workspace / "batch/labels.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(json.loads(lines[1])["output"])["label"] == "finance"
    bad = _request(workspace, content=json.dumps({"schema": BATCH_SCHEMA, "capability": "LOCAL_INFERENCE",
                                                  "items": [{}] * 65}), key="bad-batch")
    report = asyncio.run(loop.execute(bad))
    assert report.final_result.summary == "BATCH_SPEC_INVALID"
