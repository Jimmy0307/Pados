"""Closed-error boundary (CR-14 M-02): oversized numbers are refused before conversion, every conversion
error is a closed code, and nothing reachable through ``__cause__`` / ``__context__`` echoes input."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import exception_chain, put_json, run_stage, students
from pados.capability.adapter import CapabilityAdapterError
from pados.capability.artifacts import ObjectStoreArtifacts
from pados.packs.algorithms import AlgorithmAdapter
from pados.packs.algorithms.contracts import (
    DATASET_KIND,
    DATASET_SCHEMA,
    JSON_MEDIA,
    AlgorithmError,
    Dataset,
    dataset_hash,
    load_record,
    parse_dataset,
)
from pados.packs.algorithms.registry import DRSA

MARKER = "zzmarker"  # appears in every hostile payload; must never appear in any raised exception


def _numeric(cell: str) -> bytes:
    """A one-cell numeric dataset whose cell is the raw JSON literal ``cell`` (never parsed by the test)."""
    return (f'{{"schema":"{DATASET_SCHEMA}","columns":[{{"name":"{MARKER}","type":"NUMERIC"}}],'
            f'"object_ids":["{MARKER}"],"rows":[[{cell}]]}}').encode("ascii")


def _assert_closed(exc: BaseException, code: str) -> None:
    assert getattr(exc, "code", None) == code
    chain = exception_chain(exc)
    assert chain == [exc], [type(item).__name__ for item in chain]  # no __cause__, no __context__
    assert exc.__cause__ is None and exc.__context__ is None
    assert MARKER not in str(exc) and MARKER not in repr(exc) and str(exc) == code


HOSTILE = [
    ("1" + "0" * 309, "JSON_NUMBER_TOO_LARGE"),  # ~1e309 integer: float() used to raise OverflowError
    ("9" * 400, "JSON_NUMBER_TOO_LARGE"),
    ("1" + "0" * 5000, "JSON_NUMBER_TOO_LARGE"),  # beyond CPython's int-digit limit: refused before int()
    ("1" + "0" * 25, "DATASET_NUMBER_OUT_OF_RANGE"),  # short literal, value beyond |x| <= 1e15
    ("1e999", "JSON_NON_FINITE_NUMBER"),  # huge exponent overflows to infinity
    ("-1e400", "JSON_NON_FINITE_NUMBER"),
    ("1e300", "DATASET_NUMBER_OUT_OF_RANGE"),
    ("0." + "1" * 40, "JSON_NUMBER_TOO_LARGE"),
    ("NaN", "JSON_NON_FINITE_NUMBER"),
]


@pytest.mark.parametrize("cell,code", HOSTILE, ids=[c for _, c in HOSTILE])
def test_hostile_numbers_are_closed_codes_without_chain(cell: str, code: str) -> None:
    with pytest.raises(AlgorithmError) as caught:
        parse_dataset(_numeric(cell))
    _assert_closed(caught.value, code)


@pytest.mark.parametrize("cell,code", HOSTILE[:5], ids=[c for _, c in HOSTILE[:5]])
def test_hostile_numbers_through_the_adapter_boundary(tmp_path: Path, cell: str, code: str) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    source = store.put(_numeric(cell), media_type=JSON_MEDIA, kind=DATASET_KIND)
    with pytest.raises(CapabilityAdapterError) as caught:
        run_stage(store, DRSA.capability, AlgorithmAdapter(DRSA), [source], {})
    _assert_closed(caught.value, code)


def test_python_level_huge_values_never_overflow() -> None:
    document = json.loads(_numeric("1"))
    document["rows"] = [[10**400]]
    with pytest.raises(ValueError, match="DATASET_NUMBER_OUT_OF_RANGE"):
        Dataset.model_validate(document)
    document["rows"] = [[1.0]]
    document["columns"][0]["maximum"] = 1e300
    with pytest.raises(ValueError, match="DATASET_BOUNDS_INVALID"):
        Dataset.model_validate(document)


def test_negative_zero_is_normalised() -> None:
    hashes = {dataset_hash(parse_dataset(_numeric(cell))) for cell in ("0", "-0", "0.0", "-0.0", "-0e5")}
    assert len(hashes) == 1
    assert parse_dataset(_numeric("-0.0")).rows == ((0.0,),)
    assert str(parse_dataset(_numeric("-0.0")).rows[0][0]) == "0.0"


@pytest.mark.parametrize("payload,code", [
    (b'{"a":' + b"[" * 100_000 + b"]" * 100_000 + b"}", "DATASET_NOT_JSON"),  # decoder recursion
    (b'{"a":' + b"[" * 40 + b"]" * 40 + b"}", "JSON_TOO_DEEP"),
    (b'{"zzmarker": 1, "zzmarker": 2}', "JSON_DUPLICATE_KEY"),
    (b'{"zzmarker": "\xff"}', "DATASET_NOT_JSON"),
    (b'{"zzmarker": tru}', "DATASET_NOT_JSON"),
], ids=["recursive-json", "json-too-deep", "duplicate-key", "invalid-utf8", "invalid-token"])
def test_malformed_documents_are_closed_codes_without_chain(payload: bytes, code: str) -> None:
    with pytest.raises(AlgorithmError) as caught:
        parse_dataset(payload)
    _assert_closed(caught.value, code)


def test_schema_errors_carry_no_pydantic_input() -> None:
    document = students()
    document["rows"][0][0] = MARKER  # not in the ordinal domain: pydantic's error would quote the input
    with pytest.raises(AlgorithmError) as caught:
        parse_dataset(json.dumps(document).encode())
    _assert_closed(caught.value, "DATASET_VALUE_OUT_OF_DOMAIN")
    document = students()
    document[MARKER] = 1
    with pytest.raises(AlgorithmError) as caught:
        parse_dataset(json.dumps(document).encode())
    _assert_closed(caught.value, "DATASET_SCHEMA_INVALID")
    with pytest.raises(AlgorithmError) as caught:
        load_record(json.dumps({"schema": MARKER}).encode())
    _assert_closed(caught.value, "RECORD_SCHEMA_UNSUPPORTED")
    with pytest.raises(AlgorithmError) as caught:
        DRSA.parse_parameters({"criteria": [MARKER, MARKER]})
    _assert_closed(caught.value, "DRSA_CRITERIA_INVALID")


def test_credential_refusal_carries_no_path() -> None:
    document = students()
    label = "s" + "k-" + MARKER + "x" * 12  # assembled at runtime
    document["columns"][0]["domain"] = ["bad", "medium", label]
    document["rows"][0][0] = label
    with pytest.raises(AlgorithmError) as caught:
        parse_dataset(json.dumps(document).encode())
    _assert_closed(caught.value, "DATASET_CREDENTIAL_SHAPED_CONTENT")


def _boom(dataset: Dataset, parameters: Any) -> Any:
    raise RuntimeError(f"internal failure quoting {MARKER}")


def test_unexpected_adapter_exceptions_become_a_closed_code(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    source = put_json(store, students())
    broken = AlgorithmAdapter(replace(DRSA, compute=_boom))  # same descriptor, failing implementation
    with pytest.raises(CapabilityAdapterError) as caught:
        run_stage(store, DRSA.capability, broken, [source], {})
    _assert_closed(caught.value, "ALGORITHM_UNEXPECTED_ERROR")
