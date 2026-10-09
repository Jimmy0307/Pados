# SPDX-License-Identifier: AGPL-3.0-only
"""Algorithm pack contracts: datasets, results and reproducibility records (artifacts, never authority).

Datasets and results travel only as content-addressed artifacts holding canonical JSON bytes (sorted keys,
ASCII, compact separators, no NaN/Infinity). A result is a candidate (``semantic_authority = NONE``); it is
accepted only through the existing receipt -> validation -> CAS path. Every failure is a closed
``UPPER_SNAKE`` code that never echoes dataset content: public loaders raise ``AlgorithmError`` outside
the handler of the underlying exception, so neither ``__cause__`` nor ``__context__`` carries input
(JSON decoder state, pydantic input values or minimization paths).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pados.capability.artifacts import ArtifactRef
from pados.lifecycle.minimization import assert_no_credentials

DATASET_SCHEMA = "pados.algorithms.dataset.v1"
RESULT_SCHEMA = "pados.algorithms.result.v1"
RECORD_SCHEMA = "pados.algorithms.reproducibility.v1"
DATASET_KIND = "algorithms.dataset"
RESULT_KIND = "algorithms.result"
RECORD_KIND = "algorithms.reproducibility"
JSON_MEDIA = "application/json"

MAX_DATASET_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 64
MAX_CELLS = 100_000
MAX_LABEL = 64
MAX_JSON_DEPTH = 16
MAX_NUMBER_LITERAL = 32  # characters of one JSON number literal, checked before conversion
MAX_ABS_NUMBER = 1e15  # dataset numbers and bounds: |x| <= 1e15 (exact integers, no float overflow)
MAX_ABS_INT = 10**15

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_OBJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_ALGORITHM_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")  # same shape as descriptor versions
_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")

Cell = float | str | None
_Model = TypeVar("_Model", bound=BaseModel)


class AlgorithmError(ValueError):
    """Algorithm or contract failure with a closed code (no input content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validation_code(exc: ValidationError, default: str) -> str:
    """The first closed code raised by a validator, else ``default`` (pydantic text is never surfaced)."""
    for error in exc.errors():
        inner = (error.get("ctx") or {}).get("error")
        if isinstance(inner, ValueError) and _CODE.fullmatch(str(inner)):
            return str(inner)
    return default


def validated(model: type[_Model], document: Any, default: str) -> _Model:
    """``model.model_validate`` with a closed code and no exception chain (pydantic errors hold inputs)."""
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        code = validation_code(exc, default)
    except (ArithmeticError, TypeError, ValueError, RecursionError):
        code = default
    raise AlgorithmError(code)


def canonical_json(value: Any) -> bytes:
    """Canonical artifact bytes; refuses NaN/Infinity so equal content always has one encoding."""
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        text = None
    if text is None:
        raise AlgorithmError("CANONICAL_JSON_UNSERIALIZABLE")
    return text.encode("ascii")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise AlgorithmError("JSON_DUPLICATE_KEY")
    return dict(pairs)


def _refuse_constant(_: str) -> Any:
    raise AlgorithmError("JSON_NON_FINITE_NUMBER")


def _bounded_int(literal: str) -> int:
    if len(literal) > MAX_NUMBER_LITERAL:
        raise AlgorithmError("JSON_NUMBER_TOO_LARGE")
    return int(literal)


def _bounded_float(literal: str) -> float:
    if len(literal) > MAX_NUMBER_LITERAL:
        raise AlgorithmError("JSON_NUMBER_TOO_LARGE")
    number = float(literal)
    if not math.isfinite(number):  # e.g. 1e999 overflows to infinity
        raise AlgorithmError("JSON_NON_FINITE_NUMBER")
    return number


def _depth(value: Any, level: int = 0) -> None:
    if level > MAX_JSON_DEPTH:
        raise AlgorithmError("JSON_TOO_DEEP")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def parse_json_document(payload: bytes, *, max_bytes: int, code: str) -> dict[str, Any]:
    """Strict JSON object parse: bounded, UTF-8, no duplicate keys, no NaN/Infinity, bounded number
    literals (checked before conversion), bounded depth."""
    if len(payload) > max_bytes:
        raise AlgorithmError(f"{code}_TOO_LARGE")
    failure: str | None = None
    document: Any = None
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates,
                              parse_constant=_refuse_constant, parse_int=_bounded_int, parse_float=_bounded_float)
    except AlgorithmError as exc:
        failure = exc.code
    except (UnicodeDecodeError, ArithmeticError, ValueError, RecursionError, MemoryError):
        failure = f"{code}_NOT_JSON"
    if failure is not None:
        raise AlgorithmError(failure)  # outside the handler: no decoder state in the exception chain
    if not isinstance(document, dict):
        raise AlgorithmError(f"{code}_NOT_OBJECT")
    _depth(document)
    return document


def quantize(value: float, digits: int) -> float:
    """Round half-even to ``digits`` decimals (exact in CPython) and normalise -0.0 to 0.0."""
    rounded = round(float(value), digits)
    if not math.isfinite(rounded):
        raise AlgorithmError("RESULT_NUMBER_NOT_FINITE")
    return rounded + 0.0


# --- Dataset ---------------------------------------------------------------------------------------


class FeatureType(StrEnum):
    NUMERIC = "NUMERIC"
    ORDINAL = "ORDINAL"  # domain listed in increasing order of value
    NOMINAL = "NOMINAL"  # closed, unordered domain


class Criterion(StrEnum):
    """Preference semantics used by dominance: GAIN = more is better, COST = less is better."""

    GAIN = "GAIN"
    COST = "COST"
    NONE = "NONE"  # regular attribute: compared by equality only


class MissingValuePolicy(StrEnum):
    REJECT = "REJECT"
    DROP_OBJECT = "DROP_OBJECT"  # objects with any missing value are removed (count is reported)


def _label(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_LABEL or _LABEL_FORBIDDEN.search(value):
        raise ValueError("DATASET_LABEL_INVALID")
    return value


class Column(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: FeatureType
    criterion: Criterion = Criterion.NONE
    domain: tuple[str, ...] = Field(default=(), max_length=256)
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("name")
    @classmethod
    def name_pattern(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("DATASET_COLUMN_NAME_INVALID")
        return value

    @field_validator("domain")
    @classmethod
    def domain_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _label(item)
        if len(set(value)) != len(value):
            raise ValueError("DATASET_DOMAIN_INVALID")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Column:
        if self.type is FeatureType.NUMERIC:
            if self.domain:
                raise ValueError("DATASET_DOMAIN_INVALID")
            for bound in (self.minimum, self.maximum):
                if bound is not None and not (math.isfinite(bound) and abs(bound) <= MAX_ABS_NUMBER):
                    raise ValueError("DATASET_BOUNDS_INVALID")
            if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
                raise ValueError("DATASET_BOUNDS_INVALID")
        else:
            if not self.domain:
                raise ValueError("DATASET_DOMAIN_INVALID")
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("DATASET_BOUNDS_INVALID")
        if self.type is FeatureType.NOMINAL and self.criterion is not Criterion.NONE:
            raise ValueError("DATASET_NOMINAL_CANNOT_BE_CRITERION")
        return self


def _cell(value: Any) -> Cell:
    """A cell as float (|x| <= MAX_ABS_NUMBER, -0.0 normalised to 0.0), label or None; bounds are checked
    BEFORE any int -> float conversion, so a huge integer can never raise OverflowError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("DATASET_VALUE_INVALID")
    if isinstance(value, int):
        if not -MAX_ABS_INT <= value <= MAX_ABS_INT:
            raise ValueError("DATASET_NUMBER_OUT_OF_RANGE")
        return float(value) + 0.0
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("DATASET_VALUE_INVALID")
        if abs(value) > MAX_ABS_NUMBER:
            raise ValueError("DATASET_NUMBER_OUT_OF_RANGE")
        return value + 0.0
    if isinstance(value, str):
        return _label(value)
    raise ValueError("DATASET_VALUE_INVALID")


class Dataset(BaseModel):
    """A bounded table with feature metadata; rows are objects, columns are features."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default=DATASET_SCHEMA, alias="schema")
    columns: tuple[Column, ...]
    decision: str | None = None
    missing: MissingValuePolicy = MissingValuePolicy.REJECT
    object_ids: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != DATASET_SCHEMA:
            raise ValueError("DATASET_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("columns", mode="before")
    @classmethod
    def column_count(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_COLUMNS:
            raise ValueError("DATASET_COLUMNS_INVALID")
        return value

    @field_validator("object_ids", mode="before")
    @classmethod
    def object_count(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_ROWS:
            raise ValueError("DATASET_OBJECTS_INVALID")
        if any(not isinstance(item, str) or not _OBJECT_ID.fullmatch(item) for item in value):
            raise ValueError("DATASET_OBJECTS_INVALID")
        if len(set(value)) != len(value):
            raise ValueError("DATASET_OBJECTS_INVALID")
        return value

    @field_validator("rows", mode="before")
    @classmethod
    def normalised_rows(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_ROWS:
            raise ValueError("DATASET_TOO_LARGE")
        cells = 0
        rows: list[tuple[Cell, ...]] = []
        for row in value:
            if not isinstance(row, (list, tuple)):
                raise ValueError("DATASET_ROW_INVALID")
            cells += len(row)
            if cells > MAX_CELLS:
                raise ValueError("DATASET_TOO_LARGE")
            rows.append(tuple(_cell(item) for item in row))
        return tuple(rows)

    @model_validator(mode="after")
    def consistent(self) -> Dataset:
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("DATASET_COLUMNS_INVALID")
        if len(self.object_ids) != len(self.rows):
            raise ValueError("DATASET_OBJECTS_INVALID")
        if self.decision is not None and self.decision not in names:
            raise ValueError("DATASET_DECISION_UNKNOWN")
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("DATASET_ROW_WIDTH_MISMATCH")
            for column, cell in zip(self.columns, row, strict=True):
                if cell is None:
                    if self.missing is MissingValuePolicy.REJECT:
                        raise ValueError("DATASET_MISSING_VALUE")
                    continue
                _check_cell(column, cell)
        return self

    def column_index(self, name: str) -> int:
        for index, column in enumerate(self.columns):
            if column.name == name:
                return index
        raise AlgorithmError("DATASET_COLUMN_UNKNOWN")

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def _check_cell(column: Column, cell: float | str) -> None:
    if column.type is FeatureType.NUMERIC:
        if not isinstance(cell, float):
            raise ValueError("DATASET_VALUE_INVALID")
        if (column.minimum is not None and cell < column.minimum) or (
                column.maximum is not None and cell > column.maximum):
            raise ValueError("DATASET_VALUE_OUT_OF_DOMAIN")
    elif not isinstance(cell, str) or cell not in column.domain:
        raise ValueError("DATASET_VALUE_OUT_OF_DOMAIN")


def dataset_hash(dataset: Dataset) -> str:
    """Hash of the canonical serialization (encoding-independent)."""
    return canonical_digest(dataset.canonical())


def no_credentials(document: Any, code: str) -> None:
    """``assert_no_credentials`` with a closed code and no exception chain (the violation names a path)."""
    try:
        assert_no_credentials(document)
        return
    except (ValueError, RecursionError):
        pass
    raise AlgorithmError(code)


def parse_dataset(payload: bytes) -> Dataset:
    document = parse_json_document(payload, max_bytes=MAX_DATASET_BYTES, code="DATASET")
    no_credentials(document, "DATASET_CREDENTIAL_SHAPED_CONTENT")
    return validated(Dataset, document, "DATASET_SCHEMA_INVALID")


def complete_rows(dataset: Dataset) -> tuple[tuple[str, ...], tuple[tuple[float | str, ...], ...], int]:
    """Rows after the declared missing-value policy (REJECT was already enforced at validation)."""
    ids: list[str] = []
    rows: list[tuple[float | str, ...]] = []
    for object_id, row in zip(dataset.object_ids, dataset.rows, strict=True):
        if any(cell is None for cell in row):
            continue
        ids.append(object_id)
        rows.append(tuple(cell for cell in row if cell is not None))
    if not rows:
        raise AlgorithmError("DATASET_EMPTY_AFTER_MISSING_POLICY")
    return tuple(ids), tuple(rows), len(dataset.rows) - len(rows)


def square_matrix(dataset: Dataset, *, low: float, high: float, max_size: int,
                  code: str) -> tuple[tuple[str, ...], tuple[tuple[float, ...], ...]]:
    """A square numeric matrix: object ids equal column names (row i, column j = influence i -> j)."""
    names = tuple(column.name for column in dataset.columns)
    if dataset.decision is not None:
        raise AlgorithmError(f"{code}_MATRIX_HAS_DECISION")
    if len(names) > max_size:
        raise AlgorithmError(f"{code}_MATRIX_TOO_LARGE")
    if dataset.object_ids != names:
        raise AlgorithmError(f"{code}_MATRIX_NOT_SQUARE")
    if any(column.type is not FeatureType.NUMERIC for column in dataset.columns):
        raise AlgorithmError(f"{code}_MATRIX_NOT_NUMERIC")
    matrix: list[tuple[float, ...]] = []
    for row in dataset.rows:
        values: list[float] = []
        for cell in row:
            if cell is None:
                raise AlgorithmError(f"{code}_MATRIX_MISSING_VALUE")
            if not isinstance(cell, float) or not low <= cell <= high:
                raise AlgorithmError(f"{code}_MATRIX_VALUE_OUT_OF_RANGE")
            values.append(cell)
        matrix.append(tuple(values))
    return names, tuple(matrix)


# --- Results and reproducibility -------------------------------------------------------------------


def _finite_metrics(value: Mapping[str, float]) -> dict[str, float]:
    if len(value) > 32:
        raise ValueError("RESULT_METRICS_INVALID")
    cleaned: dict[str, float] = {}
    for key, number in value.items():
        if not isinstance(key, str) or not _NAME.fullmatch(key) or isinstance(number, bool) or not isinstance(
                number, (int, float)) or (isinstance(number, int) and abs(number) > MAX_ABS_INT):
            raise ValueError("RESULT_METRICS_INVALID")
        if not math.isfinite(float(number)):
            raise ValueError("RESULT_METRICS_INVALID")
        cleaned[key] = float(number) + 0.0
    return cleaned


def _pattern(value: str, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(code)
    return value


class ResultEnvelope(BaseModel):
    """The deterministic result artifact. It never contains timing, host or run-specific data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default=RESULT_SCHEMA, alias="schema")
    algorithm_id: str
    algorithm_version: str
    capability_id: str
    dataset_hash: str
    parameters_hash: str
    seed: int | None = None
    parameters: dict[str, Any]
    result: dict[str, Any]
    metrics: dict[str, float]
    semantic_authority: str = "NONE"

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != RESULT_SCHEMA:
            raise ValueError("RESULT_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("algorithm_id")
    @classmethod
    def algorithm_pattern(cls, value: str) -> str:
        return _pattern(value, _ALGORITHM_ID, "RESULT_ALGORITHM_ID_INVALID")

    @field_validator("algorithm_version")
    @classmethod
    def version_pattern(cls, value: str) -> str:
        return _pattern(value, _VERSION, "RESULT_ALGORITHM_VERSION_INVALID")

    @field_validator("capability_id")
    @classmethod
    def capability_pattern(cls, value: str) -> str:
        return _pattern(value, _CAPABILITY_ID, "RESULT_CAPABILITY_ID_INVALID")

    @field_validator("dataset_hash", "parameters_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("RESULT_HASH_INVALID")
        return value

    @field_validator("metrics")
    @classmethod
    def metrics_finite(cls, value: dict[str, float]) -> dict[str, float]:
        return _finite_metrics(value)

    @field_validator("semantic_authority")
    @classmethod
    def no_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("RESULT_CANNOT_CARRY_AUTHORITY")
        return value

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


MAX_DEADLINE_SECONDS = 3600.0  # bound of the optional wall-clock deadline (adapter / replay boundary)
RECORD_KEYS = frozenset({
    "schema", "algorithm_id", "algorithm_version", "capability_id", "adapter_id", "parameters_hash",
    "request_parameters_hash", "dataset_hash", "dataset_artifact_sha256", "seed", "result_hash", "metrics",
    "compute_target", "deadline_seconds", "completion", "semantic_authority",
})


class Completion(StrEnum):
    """Whether COMPLETING a run is a function of its inputs alone (a completed result always is)."""

    DETERMINISTIC = "DETERMINISTIC"  # no wall-clock deadline: only dataset, parameters and budgets decide
    NONDETERMINISTIC_DEADLINE = "NONDETERMINISTIC_DEADLINE"  # a deadline may abort on a slower host


class ReproducibilityRecord(BaseModel):
    """Everything needed to replay a run: ids, versions and hashes only (no dataset content)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default=RECORD_SCHEMA, alias="schema")
    algorithm_id: str
    algorithm_version: str
    capability_id: str
    adapter_id: str
    parameters_hash: str
    request_parameters_hash: str
    dataset_hash: str
    dataset_artifact_sha256: str
    seed: int | None = None
    result_hash: str
    metrics: dict[str, float]
    # No wall-clock runtime: the record is an output artifact that DETERMINISTIC_REPLAY compares byte for byte,
    # so timing lives only in the stage metrics (``runtime_ms``), never in an artifact.
    compute_target: str
    deadline_seconds: float | None = Field(default=None, gt=0.0, le=MAX_DEADLINE_SECONDS)  # telemetry
    completion: Completion = Completion.DETERMINISTIC
    semantic_authority: str = "NONE"

    @model_validator(mode="after")
    def completion_consistent(self) -> ReproducibilityRecord:
        if (self.deadline_seconds is None) != (self.completion is Completion.DETERMINISTIC):
            raise ValueError("RECORD_COMPLETION_INCONSISTENT")
        return self

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != RECORD_SCHEMA:
            raise ValueError("RECORD_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("algorithm_id")
    @classmethod
    def algorithm_pattern(cls, value: str) -> str:
        return _pattern(value, _ALGORITHM_ID, "RECORD_ALGORITHM_ID_INVALID")

    @field_validator("algorithm_version")
    @classmethod
    def version_pattern(cls, value: str) -> str:
        return _pattern(value, _VERSION, "RECORD_ALGORITHM_VERSION_INVALID")

    @field_validator("capability_id")
    @classmethod
    def capability_pattern(cls, value: str) -> str:
        return _pattern(value, _CAPABILITY_ID, "RECORD_CAPABILITY_ID_INVALID")

    @field_validator("adapter_id")
    @classmethod
    def adapter_pattern(cls, value: str) -> str:
        return _pattern(value, _ADAPTER_ID, "RECORD_ADAPTER_ID_INVALID")

    @field_validator("parameters_hash", "request_parameters_hash", "dataset_hash", "dataset_artifact_sha256",
                     "result_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("RECORD_HASH_INVALID")
        return value

    @field_validator("compute_target")
    @classmethod
    def target_pattern(cls, value: str) -> str:
        if not _ALGORITHM_ID.fullmatch(value):
            raise ValueError("RECORD_COMPUTE_TARGET_INVALID")
        return value

    @field_validator("metrics")
    @classmethod
    def metrics_finite(cls, value: dict[str, float]) -> dict[str, float]:
        return _finite_metrics(value)

    @field_validator("semantic_authority")
    @classmethod
    def no_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("RECORD_CANNOT_CARRY_AUTHORITY")
        return value

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def load_result(payload: bytes, ref: ArtifactRef) -> ResultEnvelope:
    """Consumer-side load of a result artifact; anything that is not one (e.g. an explanation) is refused."""
    if ref.kind != RESULT_KIND or ref.media_type != JSON_MEDIA:
        raise AlgorithmError("RESULT_ARTIFACT_KIND_INVALID")
    if sha256_hex(payload) != ref.sha256 or len(payload) != ref.size_bytes:
        raise AlgorithmError("RESULT_ARTIFACT_HASH_MISMATCH")
    document = parse_json_document(payload, max_bytes=MAX_RESULT_BYTES, code="RESULT")
    envelope = validated(ResultEnvelope, document, "RESULT_SCHEMA_INVALID")
    if canonical_json(envelope.canonical()) != payload:
        raise AlgorithmError("RESULT_NOT_CANONICAL")
    return envelope


def load_record(payload: bytes) -> ReproducibilityRecord:
    document = parse_json_document(payload, max_bytes=64 * 1024, code="RECORD")
    return validated(ReproducibilityRecord, document, "RECORD_SCHEMA_INVALID")


def ordered_ids(ids: Sequence[str], mask: int) -> list[str]:
    """Object ids whose bit is set in ``mask``, in dataset order."""
    return [object_id for index, object_id in enumerate(ids) if mask >> index & 1]
