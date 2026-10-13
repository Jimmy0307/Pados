# SPDX-License-Identifier: AGPL-3.0-only
"""STRUCTURED_EXTRACTION adapter: one OCR table -> one ``algorithms.dataset`` artifact (deterministic).

Input: exactly one ``ocr.document`` artifact (JSON). Parameters are flat scalars with a closed schema:

* ``table_ordinal``   - which table, counting tables page by page in document order (1-based, default 1);
* ``object_column``   - header of the column that holds the object ids;
* ``decision_column`` - optional header of the decision column (it must also be declared);
* ``column_<n>``      - ``<header>|<name>|<TYPE>|<CRITERION>[|<domain,...>]`` for n = 1..12, in dataset
  column order. TYPE is NUMERIC, ORDINAL or NOMINAL; CRITERION is GAIN, COST or NONE. ORDINAL and NOMINAL
  columns list their closed domain (ORDINAL in increasing order of value); NUMERIC columns list none.

The table header must name exactly the object column plus the declared columns: an undeclared or a
missing header fails closed. Numeric cells must be plain decimals and labels must be in the declared
domain. Numeric contract (CR-16 H-03, CR-16b): a numeric cell is accepted only when its decimal text
(``[-+]?`` up to 15 integer and 9 fraction digits) has exactly the decimal value of the SHORTEST ROUND-TRIP
``repr`` of the IEEE-754 double it parses to; downstream algorithms compute on that double. Otherwise it
fails closed with ``EXTRACTION_VALUE_NOT_EXACT`` (for example ``999999999999999.999999999``, which would
become ``1000000000000000.0``). So two cells with distinct decimal values never map to the same accepted
double, and the mapping is unambiguous and reproducible. This is NOT exact binary representation: ``0.1``
is accepted and is the double nearest to 0.1, and arithmetic on doubles rounds as IEEE-754 does. Nothing
is inferred, coerced, rounded or dropped by the extraction itself. Every failure is a closed ``EXTRACTION_*`` code raised
outside the handler, so no exception chain carries document content, and the evidence holds ids, counts
and hashes only. The output is a candidate (``semantic_authority = NONE``).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactIntegrityError, ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    Modality,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms.contracts import (
    DATASET_KIND,
    DATASET_SCHEMA,
    JSON_MEDIA,
    MAX_DATASET_BYTES,
    AlgorithmError,
    Column,
    Dataset,
    FeatureType,
    canonical_json,
    dataset_hash,
    no_credentials,
    parse_dataset,
    validated,
)
from pados.packs.algorithms.registry import DATASET_CONTRACT
from pados.packs.ocr.contracts import (
    JSON_MEDIA_TYPE,
    KIND_DOCUMENT,
    OCR_DOCUMENT_CONTRACT,
    OcrContractError,
    OcrDocument,
    Table,
)

STRUCTURED_EXTRACTION = "STRUCTURED_EXTRACTION"
EXTRACTION_ADAPTER_ID = "extraction.table-dataset"
MAX_DECLARED_COLUMNS = 12
MAX_TABLE_ORDINAL = 256
COLUMN_KEYS = tuple(f"column_{index}" for index in range(1, MAX_DECLARED_COLUMNS + 1))
PARAMETER_KEYS = frozenset({"table_ordinal", "object_column", "decision_column", *COLUMN_KEYS})
_NUMBER = re.compile(r"^[-+]?[0-9]{1,15}(\.[0-9]{1,9})?$")
_HEADER = re.compile(r"^[^\x00-\x1f\x7f|]{1,64}$")
_OBJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

CAPABILITY = CapabilityDescriptor(
    capability_id=STRUCTURED_EXTRACTION, version="1", family=CapabilityFamily.STRUCTURED_DATA,
    operation="EXTRACT_TABLE_DATASET", input_contract=OCR_DOCUMENT_CONTRACT, output_contract=DATASET_CONTRACT,
    determinism=Determinism.DETERMINISTIC, modalities=(Modality.TABLE, Modality.STRUCTURED),
    max_classification=DataClassification.RESTRICTED, network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
    failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE,
    description="One document table to a typed algorithm dataset with declared criteria")


class ExtractionError(ValueError):
    """Extraction failure with a closed code (never echoes document content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def extraction_descriptor() -> AdapterDescriptor:
    """PADOS-owned pure-Python implementation: local only, no engine, no network."""
    return AdapterDescriptor(
        adapter_id=EXTRACTION_ADAPTER_ID, capability_id=STRUCTURED_EXTRACTION, version="1", backend="python",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, license=LicenseInfo(spdx="AGPL-3.0-only"))


@dataclass(frozen=True)
class ColumnSpec:
    header: str
    column: Column


@dataclass(frozen=True)
class ExtractionSpec:
    table_ordinal: int
    object_column: str
    decision_column: str | None
    columns: tuple[ColumnSpec, ...]

    @classmethod
    def parse(cls, parameters: Mapping[str, Any]) -> ExtractionSpec:
        if not isinstance(parameters, Mapping) or not set(parameters) <= PARAMETER_KEYS:
            raise ExtractionError("EXTRACTION_PARAMETERS_INVALID")
        ordinal = parameters.get("table_ordinal", 1)
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= MAX_TABLE_ORDINAL:
            raise ExtractionError("EXTRACTION_PARAMETERS_INVALID")
        object_column = _header(parameters.get("object_column"))
        decision = parameters.get("decision_column")
        decision_column = None if decision is None else _header(decision)
        columns = tuple(_column_spec(parameters[key]) for key in COLUMN_KEYS if parameters.get(key) is not None)
        if not columns:
            raise ExtractionError("EXTRACTION_COLUMNS_MISSING")
        headers = [spec.header for spec in columns]
        names = [spec.column.name for spec in columns]
        if len(set(headers)) != len(headers) or len(set(names)) != len(names) or object_column in headers:
            raise ExtractionError("EXTRACTION_COLUMN_SPEC_INVALID")
        if decision_column is not None and decision_column not in headers:
            raise ExtractionError("EXTRACTION_DECISION_COLUMN_UNDECLARED")
        return cls(ordinal, object_column, decision_column, columns)


def _header(value: object) -> str:
    if not isinstance(value, str) or not _HEADER.fullmatch(value.strip()):
        raise ExtractionError("EXTRACTION_PARAMETERS_INVALID")
    return value.strip()


def _column_spec(value: object) -> ColumnSpec:
    if not isinstance(value, str):
        raise ExtractionError("EXTRACTION_COLUMN_SPEC_INVALID")
    parts = [part.strip() for part in value.split("|")]
    if len(parts) not in (4, 5) or not _HEADER.fullmatch(parts[0]):
        raise ExtractionError("EXTRACTION_COLUMN_SPEC_INVALID")
    domain = [item.strip() for item in parts[4].split(",")] if len(parts) == 5 else []
    document = {"name": parts[1], "type": parts[2], "criterion": parts[3], "domain": domain}
    try:
        column = validated(Column, document, "EXTRACTION_COLUMN_SPEC_INVALID")
    except AlgorithmError:
        column = None
    if column is None:  # outside the handler: no validation input in the exception chain
        raise ExtractionError("EXTRACTION_COLUMN_SPEC_INVALID")
    return ColumnSpec(parts[0], column)


def select_table(document: OcrDocument, ordinal: int) -> Table:
    """The ``ordinal``-th table, counting page by page in document order."""
    tables = [table for page in document.pages for table in page.tables]
    if ordinal > len(tables):
        raise ExtractionError("EXTRACTION_TABLE_NOT_FOUND")
    return tables[ordinal - 1]


def _cell(column: Column, text: str) -> float | str:
    if column.type is FeatureType.NUMERIC:
        if not _NUMBER.fullmatch(text):
            raise ExtractionError("EXTRACTION_VALUE_NOT_NUMERIC")
        value = float(text) + 0.0
        if Decimal(repr(value)) != Decimal(text):  # exact or refused: never silently rounded
            raise ExtractionError("EXTRACTION_VALUE_NOT_EXACT")
        return value
    if text not in column.domain:
        raise ExtractionError("EXTRACTION_VALUE_OUT_OF_DOMAIN")
    return text


def table_dataset(table: Table, spec: ExtractionSpec) -> Dataset:
    """A validated dataset from one table under the declared column specs (fail closed, no coercion)."""
    if not table.header_row:
        raise ExtractionError("EXTRACTION_TABLE_HEADER_MISSING")
    header = [cell.strip() for cell in table.cells[0]]
    if len(set(header)) != len(header):
        raise ExtractionError("EXTRACTION_HEADER_DUPLICATE")
    declared = {spec.object_column, *(item.header for item in spec.columns)}
    if any(name not in declared for name in header):
        raise ExtractionError("EXTRACTION_COLUMN_UNDECLARED")
    if any(name not in header for name in declared):
        raise ExtractionError("EXTRACTION_COLUMN_NOT_FOUND")
    rows = table.cells[1:]
    if not rows:
        raise ExtractionError("EXTRACTION_TABLE_EMPTY")
    position = {name: index for index, name in enumerate(header)}
    object_ids = [row[position[spec.object_column]].strip() for row in rows]
    if any(not _OBJECT_ID.fullmatch(item) for item in object_ids):
        raise ExtractionError("EXTRACTION_OBJECT_ID_INVALID")
    if len(set(object_ids)) != len(object_ids):
        raise ExtractionError("EXTRACTION_OBJECT_ID_DUPLICATE")
    values = [[_cell(item.column, row[position[item.header]].strip()) for item in spec.columns] for row in rows]
    decision = next((item.column.name for item in spec.columns if item.header == spec.decision_column), None)
    document = {"schema": DATASET_SCHEMA, "columns": [item.column.model_dump(mode="json") for item in spec.columns],
                "decision": decision, "missing": "REJECT", "object_ids": object_ids, "rows": values}
    try:
        dataset = validated(Dataset, document, "EXTRACTION_DATASET_INVALID")
    except AlgorithmError as exc:
        code = exc.code
    else:
        return dataset
    raise ExtractionError(code if code.startswith(("EXTRACTION_", "DATASET_")) else "EXTRACTION_DATASET_INVALID")


class TableDatasetAdapter:
    """The deterministic STRUCTURED_EXTRACTION adapter (invoked only through ``run_bound_stage``)."""

    def __init__(self) -> None:
        self._descriptor = extraction_descriptor()

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            return self._execute(request, context)
        except ArtifactAccessDenied:
            raise
        except ExtractionError as exc:
            code = exc.code
        except AlgorithmError as exc:
            code = exc.code if exc.code.startswith(("EXTRACTION_", "DATASET_")) else "EXTRACTION_DATASET_INVALID"
        except OcrContractError:
            code = "EXTRACTION_DOCUMENT_INVALID"
        except ArtifactIntegrityError:
            code = "EXTRACTION_INPUT_UNREADABLE"
        except Exception:  # noqa: BLE001 - closed-error boundary
            code = "EXTRACTION_UNEXPECTED_ERROR"
        raise CapabilityAdapterError(code) from None  # outside the handler: empty exception chain

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.capability_id != STRUCTURED_EXTRACTION:
            raise ExtractionError("EXTRACTION_CAPABILITY_MISMATCH")
        if request.seed is not None:
            raise ExtractionError("EXTRACTION_SEED_NOT_APPLICABLE")
        if len(request.inputs) != 1:
            raise ExtractionError("EXTRACTION_INPUT_COUNT_INVALID")
        source = request.inputs[0]
        if source.kind != KIND_DOCUMENT or source.media_type != JSON_MEDIA_TYPE:
            raise ExtractionError("EXTRACTION_INPUT_NOT_DOCUMENT")
        spec = ExtractionSpec.parse(request.parameters)
        document = OcrDocument.from_bytes(context.artifacts.get(source))
        table = select_table(document, spec.table_ordinal)
        dataset = table_dataset(table, spec)
        canonical = dataset.canonical()
        no_credentials(canonical, "EXTRACTION_CREDENTIAL_SHAPED_CONTENT")
        payload = canonical_json(canonical)
        if len(payload) > MAX_DATASET_BYTES:
            raise ExtractionError("EXTRACTION_DATASET_TOO_LARGE")
        ref = context.artifacts.put(payload, media_type=JSON_MEDIA, kind=DATASET_KIND)
        evidence: dict[str, str | int | float | bool | None] = {
            "output_contract": DATASET_CONTRACT, "input_document_sha256": source.sha256,
            "source_sha256": document.source.sha256, "table_id": table.table_id, "table_rows": table.rows,
            "table_columns": table.columns, "objects": len(dataset.object_ids), "columns": len(dataset.columns),
            "decision_declared": dataset.decision is not None, "dataset_sha256": ref.sha256,
            "dataset_hash": dataset_hash(dataset), "adapter_id": EXTRACTION_ADAPTER_ID,
        }
        return StageOutput(outputs=(ref,), metrics={"objects": float(len(dataset.object_ids)),
                                                    "columns": float(len(dataset.columns))}, evidence=evidence)


def dataset_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``algorithms.dataset/v1``: one canonical, contract-valid dataset artifact."""
    if len(outputs) != 1:
        return False
    ref, payload = outputs[0]
    if ref.kind != DATASET_KIND or ref.media_type != JSON_MEDIA:
        return False
    try:
        dataset = parse_dataset(payload)
    except AlgorithmError:
        return False
    return canonical_json(dataset.canonical()) == payload
