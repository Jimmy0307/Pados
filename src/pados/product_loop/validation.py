# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pados.execution.receipts import verify_execution_receipt, verify_handoff_files
from pados.execution.safe_fs import MAX_FILE_BYTES, SafeFSError, workspace_fs
from pados.product_loop.schemas import (
    AcceptanceCriterion,
    CheckKind,
    CriterionFinding,
    FailureClass,
    RuntimeResult,
    RuntimeStatus,
    ValidationOperation,
    ValidationReport,
    ValidationStatus,
    WorkContract,
)


class Validator(Protocol):
    async def validate(self, contract: WorkContract, runtime_result: RuntimeResult) -> ValidationReport:
        """Evaluate the declared acceptance and validation contract."""


class FileValidator:
    """Read-only validator for bounded file and compile criteria."""

    async def validate(self, contract: WorkContract, runtime_result: RuntimeResult) -> ValidationReport:
        validation_id = f"validation-{uuid4()}"
        if runtime_result.status != RuntimeStatus.COMPLETED:
            status = (
                ValidationStatus.BLOCKED
                if runtime_result.status == RuntimeStatus.BLOCKED
                else ValidationStatus.NOT_RUN
            )
            return ValidationReport(
                validation_id=validation_id,
                status=status,
                validation_passed=False,
                technical_test_passed=None,
                failure_classification=runtime_result.failure_classification,
            )

        if not verify_execution_receipt(
            runtime_result.execution_receipt, run_id=runtime_result.run_id,
            attempt=runtime_result.attempt, contract=contract.model_dump(mode="json"),
            artifacts=runtime_result.artifact_refs,
        ) or not verify_handoff_files(
            contract.scope.workspace_root, runtime_result.artifact_refs,
            [change.path for change in contract.changes],
        ):
            return ValidationReport(
                validation_id=validation_id, status=ValidationStatus.BLOCKED,
                validation_passed=False, failure_classification=FailureClass.EVIDENCE_GAP,
                criterion_findings=[CriterionFinding(
                    criterion_id="execution-receipt", status=ValidationStatus.BLOCKED,
                    detail="ExecutionReceipt missing, mismatched, or simulated.",
                )],
            )

        root = Path(contract.scope.workspace_root).resolve()
        findings = [
            self._check(root, contract, criterion, runtime_result)
            for criterion in contract.acceptance_criteria
        ]
        required = [finding for finding, criterion in zip(findings, contract.acceptance_criteria) if criterion.required]
        all_required_pass = bool(required) and all(finding.status == ValidationStatus.PASSED for finding in required)
        technical_findings = [
            finding
            for finding, criterion in zip(findings, contract.acceptance_criteria)
            if criterion.operation == ValidationOperation.TEST
        ]
        technical_passed: bool | None = (
            all(finding.status == ValidationStatus.PASSED for finding in technical_findings)
            if technical_findings
            else None
        )
        status = ValidationStatus.PASSED if all_required_pass else ValidationStatus.FAILED
        blocked = any(finding.status == ValidationStatus.BLOCKED for finding in findings)
        failure = (
            None
            if all_required_pass
            else FailureClass.AUTHORITY_BLOCK
            if blocked
            else FailureClass.IMPLEMENTATION_DEFECT
        )
        return ValidationReport(
            validation_id=validation_id,
            status=status,
            criterion_findings=findings,
            technical_test_passed=technical_passed,
            validation_passed=all_required_pass,
            failure_classification=failure,
        )

    def _check(
        self,
        root: Path,
        contract: WorkContract,
        criterion: AcceptanceCriterion,
        runtime_result: RuntimeResult,
    ) -> CriterionFinding:
        if criterion.check == CheckKind.CHANGED_PATHS_SCOPED:
            declared = {change.path for change in contract.changes}
            returned = set(runtime_result.changed_paths)
            passed = returned == declared
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                detail="changed paths are within the returned runtime envelope"
                if passed
                else "runtime changed paths are inconsistent",
                evidence={"changed_paths": sorted(returned), "declared_paths": sorted(declared)},
            )
        if criterion.target_path is None:
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.FAILED,
                detail="criterion has no target path",
            )
        if criterion.target_path not in contract.scope.allowed_paths:
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.BLOCKED,
                detail="criterion target is outside the contract allowlist",
                evidence={"target_path": criterion.target_path},
            )
        target = root / criterion.target_path
        fs = workspace_fs(root)
        try:
            payload = fs.read_file(criterion.target_path, max_bytes=MAX_FILE_BYTES)
        except SafeFSError as exc:
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.BLOCKED,
                detail=f"criterion target is not a confined regular file ({exc.code})",
                evidence={"target_path": criterion.target_path},
            )
        if criterion.check == CheckKind.FILE_EXISTS:
            passed = payload is not None
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                detail="file exists" if passed else "file does not exist",
                evidence={"path": criterion.target_path},
            )
        if payload is None:
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.FAILED,
                detail="target file does not exist",
                evidence={"path": criterion.target_path},
            )
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.FAILED,
                detail=f"target could not be read: {exc}",
            )
        if criterion.check == CheckKind.FILE_CONTAINS:
            assert criterion.expected_text is not None
            passed = criterion.expected_text in content
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                detail="expected text found" if passed else "expected text missing",
                evidence={
                    "path": criterion.target_path,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                },
            )
        if criterion.check == CheckKind.PYTHON_COMPILE:
            try:
                compile(content, str(target), "exec")
                status = ValidationStatus.PASSED
                detail = "python compile check passed"
            except SyntaxError as exc:
                status = ValidationStatus.FAILED
                detail = f"python compile check failed: {exc.msg}"
            return CriterionFinding(
                criterion_id=criterion.criterion_id,
                status=status,
                detail=detail,
                evidence={"path": criterion.target_path},
            )
        return CriterionFinding(
            criterion_id=criterion.criterion_id,
            status=ValidationStatus.BLOCKED,
            detail=f"unsupported check: {criterion.check.value}",
        )
