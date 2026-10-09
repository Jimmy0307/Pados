# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pados.execution.context import ProjectContextLoader
from pados.product_loop.authorization import (
    ProductAuthorizationError,
    resolve_approved_workspace_root,
)
from pados.product_loop.maker import DirectMaker
from pados.product_loop.read_model import build_durable_read, contract_summary
from pados.product_loop.schemas import (
    AttemptRecord,
    CognitiveComposition,
    ContractState,
    Decision,
    Design,
    DurableProductRead,
    FailureClass,
    FileChange,
    Goal,
    GoalRequest,
    Intent,
    ProductLoopReport,
    ProductLoopStatus,
    Reflection,
    RetryPolicy,
    RuntimeResult,
    RuntimeStatus,
    ScopeSpec,
    ValidationReport,
    ValidationStatus,
    WorkContract,
)
from pados.product_loop.state import ReceiptGateway
from pados.product_loop.validation import Validator


class DeterministicComposer:
    """Small fixed policy for the first product; no model-generated authority routing."""

    def compose(
        self,
        *,
        goal: Goal,
        intent: Intent,
        request: GoalRequest,
    ) -> tuple[CognitiveComposition, Design, Decision]:
        composition = CognitiveComposition(
            composition_id=f"composition-{uuid4()}",
            version=1,
            selected_capabilities=["UNDERSTAND", "DESIGN", "DECIDE", "REFLECT"],
            dependencies=[
                "UNDERSTAND -> DESIGN",
                "DESIGN -> DECIDE",
                "DECIDE -> MAKE",
                "MAKE/TEST -> REFLECT",
            ],
            evidence_requirements=["EXPLICIT_EVIDENCE_OR_NONE"],
            stop_conditions=[
                "AUTHORITY_BLOCK",
                "UNSAFE_SCOPE",
                "VALIDATION_FAILURE_WITHOUT_BOUNDED_REVISION",
                "CAS_STALE",
                "ATTEMPT_BUDGET_EXHAUSTED",
            ],
            max_rounds=request.max_attempts,
            handoff_conditions=[
                "DECISION_TO_WORK_CONTRACT",
                "REFLECTION_TO_BOUNDED_REVISION",
                "VERIFY_TO_COMMIT_OR_HUMAN_REVIEW",
            ],
            risk_policy="REVERSIBLE_ISOLATED_FILE_CHANGE_ONLY",
        )
        design = Design(
            design_id=f"design-{uuid4()}",
            approach="Apply declared file changes in one approved isolated workspace and run declared checks.",
            alternatives_rejected=[
                "EXTERNAL_ORCHESTRATION_NOT_NEEDED_FOR_ONE_DIRECT_MAKER",
                "AUTOMATIC_MEMORY_PROMOTION_NOT_ALLOWED",
            ],
            acceptance_shape=[criterion.criterion_id for criterion in request.acceptance_criteria],
            constraints=[
                "CANDIDATE_WORKSPACE_ONLY",
                "NO_MERGE_PUSH_DEPLOY",
                "NO_AUTHORITY_BYPASS",
            ],
        )
        decision = Decision(
            decision_id=f"decision-{uuid4()}",
            accepted_direction="DIRECT_MAKER_IN_APPROVED_ISOLATED_WORKSPACE",
            authority="HUMAN_GATE_REQUIRED" if request.human_gate else "GOVERNED_BOUNDED",
            human_gate_required=request.human_gate,
            base_scope_version=0,
            rationale=f"Bounded implementation selected for Goal {goal.goal_id} and Intent {intent.intent_id}.",
        )
        return composition, design, decision


class DeterministicReflector:
    def reflect(
        self,
        *,
        attempt: int,
        validation: ValidationReport,
        runtime_result: RuntimeResult,
        can_revise: bool,
    ) -> Reflection:
        failure = validation.failure_classification or runtime_result.failure_classification
        if validation.validation_passed:
            return Reflection(
                reflection_id=f"reflection-{uuid4()}",
                attempt=attempt,
                failure_classification=None,
                progress="Validation criteria passed.",
                next_action="VERIFY",
                stop_reason="VALIDATION_PASSED",
            )
        if can_revise:
            return Reflection(
                reflection_id=f"reflection-{uuid4()}",
                attempt=attempt,
                failure_classification=failure,
                progress="Attempt returned evidence but did not satisfy the declared criteria.",
                next_action="REVISE_BOUNDED_WORK_CONTRACT",
                revision_created=True,
            )
        return Reflection(
            reflection_id=f"reflection-{uuid4()}",
            attempt=attempt,
            failure_classification=failure,
            progress="Attempt evidence retained; no permitted revision remains.",
            next_action="STOP_AND_REPORT",
            stop_reason="REVISION_NOT_AVAILABLE_OR_BUDGET_EXHAUSTED",
        )


class ContextExtension(Protocol):
    """Server-side bounded context provider; its output is data, never permission."""

    name: str

    def resolve(self, *, scope_id: str, goal: str) -> dict[str, Any]: ...


class ProductLoopService:
    def __init__(
        self,
        *,
        maker: DirectMaker,
        validator: Validator,
        state_gateway: ReceiptGateway,
        composer: DeterministicComposer | None = None,
        reflector: DeterministicReflector | None = None,
        approved_workspace_root: Path | str | None = None,
        context_extensions: Sequence[ContextExtension] = (),
    ) -> None:
        self.maker = maker
        self.validator = validator
        self.state_gateway = state_gateway
        self.composer = composer or DeterministicComposer()
        self.reflector = reflector or DeterministicReflector()
        self.approved_workspace_root = (
            None if approved_workspace_root is None else Path(approved_workspace_root)
        )
        self._completed_by_idempotency: dict[str, ProductLoopReport] = {}
        # Server-side, optional context providers (e.g. enterprise evidence).
        # Empty by default, so the Core context is unchanged.
        self.context_extensions = tuple(context_extensions)

    def _resolve_extensions(self, request: GoalRequest) -> dict[str, Any]:
        """Resolve extension context before any run exists; a denial fails closed."""
        resolved: dict[str, Any] = {}
        for extension in self.context_extensions:
            try:
                resolved[extension.name] = extension.resolve(scope_id=request.scope_id, goal=request.goal)
            except PermissionError as exc:
                raise ProductAuthorizationError("CONTEXT_EXTENSION_UNAUTHORIZED") from exc
        return resolved

    def read_run(self, run_id: str) -> DurableProductRead | None:
        """Return a durable read rebuilt from the state substrate."""
        return self.state_gateway.read_run(run_id)

    def read_run_scope(self, run_id: str) -> str | None:
        """Return the durable scope binding for one run without disclosing it."""
        return self.state_gateway.read_run_scope(run_id)

    def assert_workspace_root_approved(self, request: GoalRequest) -> None:
        """Reject a caller-selected workspace root before any durable side effect.

        Defense in depth only: the Direct Maker keeps its own independent
        approved-root check, which this must never replace or weaken.
        """
        if self.approved_workspace_root is None:
            return
        resolve_approved_workspace_root(request.workspace_root, self.approved_workspace_root)

    async def execute(self, request: GoalRequest) -> ProductLoopReport:
        existing = self._completed_by_idempotency.get(request.idempotency_key)
        if existing is not None:
            return existing
        # Workspace authority is settled before the run identifier exists, so a
        # rejected root creates no run context, no read index row, no snapshot,
        # no receipt, and no Maker invocation.
        self.assert_workspace_root_approved(request)
        project_context_resolver = getattr(self.state_gateway, "resolve_project_context", None)
        if callable(project_context_resolver):
            try:
                semantic = project_context_resolver(request.scope_id)
            except ValueError as exc:
                raise ProductAuthorizationError("SEMANTIC_CONTEXT_UNAVAILABLE") from exc
            registered_root = semantic.get("project", {}).get("root_path")
            if not registered_root or Path(str(registered_root)).resolve() != Path(request.workspace_root).resolve():
                raise ProductAuthorizationError("REGISTERED_PROJECT_ROOT_MISMATCH")
        else:
            semantic = self.state_gateway.semantic_context(request.scope_id)
        if request.approval_ref is not None:
            verify_approval = getattr(self.state_gateway, "verify_approval", None)
            if not callable(verify_approval):
                raise ProductAuthorizationError("APPROVAL_VERIFICATION_UNAVAILABLE")
            verify_approval(request.approval_ref, request.scope_id)
        if self.approved_workspace_root is not None:
            context = ProjectContextLoader(self.approved_workspace_root).load(
                scope_id=request.scope_id, semantic=semantic,
            )
            bounded = context.to_dict()
            if self.context_extensions:
                bounded["extensions"] = self._resolve_extensions(request)
            request = request.model_copy(update={"context": bounded})
        run_id = str(uuid4())
        started_at = _now()
        goal = Goal(goal_id=f"goal-{uuid4()}", objective=request.goal, scope_id=request.scope_id)
        intent = Intent(
            intent_id=f"intent-{uuid4()}",
            original_request=request.goal,
            objective=request.goal,
            scope_id=request.scope_id,
        )
        composition, design, decision = self.composer.compose(
            goal=goal,
            intent=intent,
            request=request,
        )
        decision = decision.model_copy(update={"base_scope_version": semantic["scope_version"]})
        contract = self._build_contract(
            run_id=run_id,
            goal=goal,
            intent=intent,
            decision=decision,
            request=request,
        )
        await self.state_gateway.persist_run_context(
            self._run_context_payload(
                run_id=run_id,
                started_at=started_at,
                request=request,
                goal=goal,
                intent=intent,
                composition=composition,
                design=design,
                decision=decision,
                contract=contract,
            )
        )
        await self._persist_read_snapshot(
            run_id=run_id,
            started_at=started_at,
            status="WORKING",
            current_phase="AUTHORIZED",
            goal=goal,
            intent=intent,
            composition=composition,
            design=design,
            decision=decision,
            contracts=[contract],
            attempts=[],
            reflections=[],
            final_result=None,
            final_validation=None,
            commit=None,
        )
        if not request.authorized:
            blocked_contract = contract.model_copy(update={"state": ContractState.READY})
            report = self._report(
                status=ProductLoopStatus.AUTHORITY_BLOCK,
                run_id=run_id,
                goal=goal,
                intent=intent,
                composition=composition,
                design=design,
                decision=decision,
                contracts=[blocked_contract],
                attempts=[],
                reflections=[],
                final_result=None,
                final_validation=None,
                user_report="Work is ready but not authorized for dispatch.",
                semantic_state="NOT_COMMITTED",
                human_decision_required=True,
            )
            return await self._finalize_report(report)

        contracts = [contract]
        attempts: list[AttemptRecord] = []
        reflections: list[Reflection] = []
        current_contract = contract
        commit: dict[str, Any] | None = None
        final_projection: dict[str, Any] | None = None
        final_result: RuntimeResult | None = None
        final_validation: ValidationReport | None = None
        status = ProductLoopStatus.REJECTED
        semantic_state = "NOT_COMMITTED"

        for attempt in range(1, request.max_attempts + 1):
            final_result = await self.maker.make(current_contract, run_id=run_id, attempt=attempt)
            receipt = await self.state_gateway.record_attempt(
                run_id=run_id,
                attempt=attempt,
                contract=current_contract,
                runtime_result=final_result,
            )
            pending_validation = ValidationReport(
                validation_id=f"pending-{run_id}-{attempt}",
                status=ValidationStatus.NOT_RUN,
                validation_passed=False,
                technical_test_passed=None,
                failure_classification=final_result.failure_classification,
            )
            await self._persist_read_snapshot(
                run_id=run_id,
                started_at=started_at,
                status=(
                    "AUTHORITY_BLOCK"
                    if final_result.status == RuntimeStatus.BLOCKED
                    else "INCOMPLETE"
                ),
                current_phase=(
                    "AUTHORITY_BLOCK"
                    if final_result.status == RuntimeStatus.BLOCKED
                    else "RESULT_RECEIVED"
                ),
                goal=goal,
                intent=intent,
                composition=composition,
                design=design,
                decision=decision,
                contracts=contracts,
                attempts=(*attempts, AttemptRecord(
                    attempt=attempt,
                    contract_id=current_contract.work_contract_id,
                    runtime_result=final_result,
                    receipt=receipt,
                    validation=pending_validation,
                )),
                reflections=reflections,
                final_result=final_result,
                final_validation=pending_validation,
                commit=None,
                human_action_required=final_result.status == RuntimeStatus.BLOCKED,
            )
            validation = await self.validator.validate(current_contract, final_result)
            final_validation = validation
            can_revise = (
                not validation.validation_passed
                and request.max_attempts > attempt
                and bool(request.revision_changes)
                and current_contract.retry_policy.allow_bounded_revision
                and len(reflections) < current_contract.retry_policy.max_revisions
                and validation.failure_classification == FailureClass.IMPLEMENTATION_DEFECT
            )
            reflection = self.reflector.reflect(
                attempt=attempt,
                validation=validation,
                runtime_result=final_result,
                can_revise=can_revise,
            )
            reflections.append(reflection)
            attempt_record = AttemptRecord(
                attempt=attempt,
                contract_id=current_contract.work_contract_id,
                runtime_result=final_result,
                receipt=receipt,
                validation=validation,
            )
            attempts.append(attempt_record)
            observation_status = (
                "AUTHORITY_BLOCK"
                if final_result.status == RuntimeStatus.BLOCKED
                else "VERIFIED"
                if validation.validation_passed
                else "CORRECTED"
                if can_revise
                else "ITERATION_LIMIT"
                if attempt >= request.max_attempts
                else "REJECTED"
            )
            await self._persist_read_snapshot(
                run_id=run_id,
                started_at=started_at,
                status=observation_status,
                current_phase=(
                    "AUTHORITY_BLOCK"
                    if final_result.status == RuntimeStatus.BLOCKED
                    else "VERIFY"
                    if validation.validation_passed
                    else "REFLECT"
                ),
                goal=goal,
                intent=intent,
                composition=composition,
                design=design,
                decision=decision,
                contracts=contracts,
                attempts=attempts,
                reflections=reflections,
                final_result=final_result,
                final_validation=validation,
                commit=None,
                human_action_required=(
                    final_result.status == RuntimeStatus.BLOCKED or current_contract.human_gate_required
                ),
                automatic_continuation_allowed=can_revise,
            )

            if final_result.status == RuntimeStatus.BLOCKED:
                status = ProductLoopStatus.AUTHORITY_BLOCK
                semantic_state = "NOT_COMMITTED"
                break
            if not validation.validation_passed:
                if can_revise:
                    current_contract = self._revise_contract(current_contract, request.revision_changes)
                    contracts.append(current_contract)
                    await self._persist_read_snapshot(
                        run_id=run_id,
                        started_at=started_at,
                        status="CORRECTED",
                        current_phase="REVISE",
                        goal=goal,
                        intent=intent,
                        composition=composition,
                        design=design,
                        decision=decision,
                        contracts=contracts,
                        attempts=attempts,
                        reflections=reflections,
                        final_result=final_result,
                        final_validation=final_validation,
                        commit=None,
                        automatic_continuation_allowed=True,
                    )
                    continue
                status = ProductLoopStatus.REJECTED
                semantic_state = "NOT_COMMITTED"
                break
            if current_contract.human_gate_required:
                status = ProductLoopStatus.HUMAN_REVIEW_REQUIRED
                semantic_state = "ACCEPTED_PENDING_HUMAN_DECISION"
                final_validation = validation.model_copy(update={"semantic_accepted": False})
                break

            commit = await self.state_gateway.commit(receipt=receipt, contract=current_contract)
            attempts[-1] = attempt_record.model_copy(
                update={"receipt": receipt.model_copy(update={"commit": commit})}
            )
            if commit.get("status") == "STALE_CANDIDATE":
                status = ProductLoopStatus.STALE_CANDIDATE
                semantic_state = "STALE"
                final_validation = validation.model_copy(update={"semantic_accepted": False})
                await self._persist_read_snapshot(
                    run_id=run_id,
                    started_at=started_at,
                    status="STALE_CANDIDATE",
                    current_phase="STALE",
                    goal=goal,
                    intent=intent,
                    composition=composition,
                    design=design,
                    decision=decision,
                    contracts=contracts,
                    attempts=attempts,
                    reflections=reflections,
                    final_result=final_result,
                    final_validation=final_validation,
                    commit=commit,
                    human_action_required=True,
                )
                break
            if commit.get("status") != "COMMITTED":
                status = ProductLoopStatus.REJECTED
                semantic_state = "NOT_COMMITTED"
                break
            final_projection = await self.state_gateway.rebuild_projection(
                receipt=receipt,
                contract=current_contract,
            )
            status = ProductLoopStatus.COMMITTED
            semantic_state = "COMMITTED"
            final_validation = validation.model_copy(
                update={"semantic_accepted": True, "human_approved": False}
            )
            attempts[-1] = attempt_record.model_copy(
                update={
                    "validation": final_validation,
                    "receipt": receipt.model_copy(update={"commit": commit, "projection": final_projection}),
                }
            )
            await self._persist_read_snapshot(
                run_id=run_id,
                started_at=started_at,
                status="COMMITTED",
                current_phase="ACCEPTED",
                goal=goal,
                intent=intent,
                composition=composition,
                design=design,
                decision=decision,
                contracts=contracts,
                attempts=attempts,
                reflections=reflections,
                final_result=final_result,
                final_validation=final_validation,
                commit=commit,
            )
            break

        if final_result is not None and final_result.status == RuntimeStatus.COMPLETED:
            contracts[-1] = contracts[-1].model_copy(
                update={
                    "state": (
                        ContractState.ACCEPTED
                        if status == ProductLoopStatus.COMMITTED
                        else ContractState.REJECTED
                    )
                }
            )
        user_report = self._user_report(
            status=status,
            goal=goal,
            attempts=attempts,
            validation=final_validation,
            commit=commit,
            human_gate=decision.human_gate_required,
            projection=final_projection,
        )
        report = self._report(
            status=status,
            run_id=run_id,
            goal=goal,
            intent=intent,
            composition=composition,
            design=design,
            decision=decision,
            contracts=contracts,
            attempts=attempts,
            reflections=reflections,
            final_result=final_result,
            final_validation=final_validation,
            user_report=user_report,
            semantic_state=semantic_state,
            commit=commit,
            human_decision_required=decision.human_gate_required,
        )
        return await self._finalize_report(report)

    async def _finalize_report(self, report: ProductLoopReport) -> ProductLoopReport:
        report_ref = await self.state_gateway.persist_report(
            run_id=report.run_id,
            payload=report.model_dump(mode="json"),
        )
        finalized = report.model_copy(update={"report_object_ref": report_ref})
        self._completed_by_idempotency[report.contracts[0].idempotency_key] = finalized
        return finalized

    async def _persist_read_snapshot(
        self,
        *,
        run_id: str,
        started_at: str,
        status: str,
        current_phase: str,
        goal: Goal,
        intent: Intent,
        composition: CognitiveComposition,
        design: Design,
        decision: Decision,
        contracts: list[WorkContract],
        attempts: list[AttemptRecord] | tuple[AttemptRecord, ...],
        reflections: list[Reflection],
        final_result: RuntimeResult | None,
        final_validation: ValidationReport | None,
        commit: Mapping[str, Any] | None,
        human_action_required: bool = False,
        automatic_continuation_allowed: bool = False,
    ) -> None:
        read = build_durable_read(
            run_id=run_id,
            status=status,
            goal=goal,
            intent=intent,
            composition=composition,
            design=design,
            decision=decision,
            contracts=contracts,
            attempts=attempts,
            reflections=reflections,
            final_result=final_result,
            final_validation=final_validation,
            commit=commit,
            started_at=started_at,
            current_phase=current_phase,
            human_action_required=human_action_required,
            automatic_continuation_allowed=automatic_continuation_allowed,
        )
        await self.state_gateway.persist_read_snapshot(
            run_id=run_id,
            payload=read.model_dump(mode="json"),
        )

    def _run_context_payload(
        self,
        *,
        run_id: str,
        started_at: str,
        request: GoalRequest,
        goal: Goal,
        intent: Intent,
        composition: CognitiveComposition,
        design: Design,
        decision: Decision,
        contract: WorkContract,
    ) -> dict[str, Any]:
        return {
            "schema": "P6_21H_RUN_CONTEXT_V1",
            "run_id": run_id,
            "started_at": started_at,
            "scope_id": request.scope_id,
            "idempotency_key": request.idempotency_key,
            "authorized": request.authorized,
            "goal": goal.model_dump(mode="json"),
            "intent": intent.model_dump(mode="json"),
            "composition": composition.model_dump(mode="json"),
            "design": design.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "contract": contract_summary(contract).model_dump(mode="json"),
            "bounded_context": request.context if self.approved_workspace_root is not None else {},
        }

    def _build_contract(
        self,
        *,
        run_id: str,
        goal: Goal,
        intent: Intent,
        decision: Decision,
        request: GoalRequest,
    ) -> WorkContract:
        fingerprint = hashlib.sha256(
            json.dumps(request.context, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        allowed_paths = tuple(change.path for change in request.changes)
        return WorkContract(
            work_contract_id=f"contract-{uuid4()}",
            version=1,
            parent_goal=goal.goal_id,
            intent=intent,
            decision_ref=decision.decision_id,
            objective=request.goal,
            scope=ScopeSpec(
                workspace_root=str(Path(request.workspace_root).resolve()),
                allowed_paths=allowed_paths,
                isolated=True,
            ),
            constraints=(
                "APPROVED_WORKSPACE_ONLY",
                "DECLARED_PATHS_ONLY",
                "NO_MERGE_PUSH_DEPLOY",
                "NO_MEMORY_PROMOTION",
                "NO_GRAPH_CONFIRMATION",
            ),
            context_ref="goal-request-context",
            context_fingerprint=fingerprint,
            context_snapshot=request.context,
            semantic_base_scope_version=decision.base_scope_version,
            required_capabilities=("MAKE", "TEST", "ACCEPTANCE_CHECK", "REFLECT"),
            allowed_actions=("MAKE", "WRITE_APPROVED_WORKSPACE", "READ_APPROVED_WORKSPACE"),
            prohibited_actions=(
                "WRITE_FORMAL_REPO",
                "MERGE",
                "PUSH",
                "DEPLOY",
                "PROMOTE_MEMORY",
                "CONFIRM_GRAPH",
            ),
            side_effect_policy="ISOLATED_WORKSPACE_ONLY",
            implementation_class="DIRECT_FILE_MAKER",
            runtime_policy="NATIVE_DIRECT_MAKER_SINGLE_ATTEMPT",
            changes=tuple(request.changes),
            acceptance_criteria=tuple(request.acceptance_criteria),
            validation_policy="DECLARED_READ_ONLY_FILE_AND_COMPILE_CHECKS",
            retry_policy=RetryPolicy(
                max_attempts=request.max_attempts,
                allow_bounded_revision=bool(request.revision_changes),
                max_revisions=1 if request.revision_changes else 0,
            ),
            idempotency_key=request.idempotency_key,
            correlation_id=run_id,
            human_gate_required=request.human_gate,
            approval_ref=request.approval_ref,
        )

    def _revise_contract(self, current: WorkContract, changes: list[FileChange]) -> WorkContract:
        return current.model_copy(
            update={
                "work_contract_id": f"contract-{uuid4()}",
                "parent_contract_id": current.work_contract_id,
                "version": current.version + 1,
                "changes": tuple(changes),
                "scope": current.scope.model_copy(
                    update={"allowed_paths": tuple(change.path for change in changes)}
                ),
                "state": ContractState.AUTHORIZED,
            }
        )

    def _report(
        self,
        *,
        status: ProductLoopStatus,
        run_id: str,
        goal: Goal,
        intent: Intent,
        composition: CognitiveComposition,
        design: Design,
        decision: Decision,
        contracts: list[WorkContract],
        attempts: list[AttemptRecord],
        reflections: list[Reflection],
        final_result: RuntimeResult | None,
        final_validation: ValidationReport | None,
        user_report: str,
        semantic_state: str,
        commit: dict[str, Any] | None = None,
        human_decision_required: bool = False,
    ) -> ProductLoopReport:
        return ProductLoopReport(
            run_id=run_id,
            status=status,
            goal=goal,
            intent=intent,
            composition=composition,
            design=design,
            decision=decision,
            contracts=contracts,
            attempts=attempts,
            reflections=reflections,
            final_result=final_result,
            final_validation=final_validation,
            user_report=user_report,
            semantic_state=semantic_state,
            commit=commit,
            human_decision_required=human_decision_required,
        )

    def _user_report(
        self,
        *,
        status: ProductLoopStatus,
        goal: Goal,
        attempts: list[AttemptRecord],
        validation: ValidationReport | None,
        commit: Mapping[str, Any] | None,
        human_gate: bool,
        projection: Mapping[str, Any] | None,
    ) -> str:
        checks = "not run" if validation is None else validation.status.value
        commit_status = "not committed" if commit is None else str(commit.get("status"))
        revision = max(0, len(attempts) - 1)
        gate = "human decision required" if human_gate else "no human gate configured"
        projection_state = "available" if projection is not None else "not materialized"
        return (
            f"Goal: {goal.objective}\n"
            f"Work status: {status.value}\n"
            f"Attempts: {len(attempts)}; bounded revisions: {revision}\n"
            f"Checks: {checks}; semantic change: {commit_status}\n"
            f"Authority: {gate}; projection: {projection_state}\n"
            "Memory: CANDIDATE only. Graph: PROPOSAL_ONLY."
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()
