# SPDX-License-Identifier: AGPL-3.0-only
"""Operator model pins: a local model directory is usable only when every pinned file matches byte for byte.

Nothing here downloads: verification reads the operator's directory and refuses with a closed code on any
missing, resized, altered, symlinked or escaping file, a non-allowlisted license, or an unpinned role.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from pados.packs.document_intelligence.model_pin import (
    BUILTIN_PINS,
    ModelFile,
    ModelPin,
    ModelPinError,
    ModelRole,
    load_pin,
    verify_model_dir,
)


def _pin(tmp_path: Path, contents: dict[str, bytes], **overrides: object) -> tuple[ModelPin, Path]:
    root = tmp_path / "model"
    for name, data in contents.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    values: dict[str, object] = dict(
        pin_id="test-embed", role=ModelRole.EMBEDDING, engine="fastembed", model_name="BAAI/bge-small-en-v1.5",
        source_repo="Qdrant/bge-small-en-v1.5-onnx-Q", revision="a" * 40, license_spdx="MIT",
        files=tuple(ModelFile(path=name, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                    for name, data in sorted(contents.items())),
        dimension=4, max_batch=8, max_input_chars=2000, timeout_seconds=30.0, threads=1, max_ram_mb=1024)
    values.update(overrides)
    return ModelPin(**values), root


def test_a_matching_directory_verifies(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, {"model.onnx": b"onnx", "tokenizer.json": b"{}"})
    assert verify_model_dir(pin, root) is None


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda root: (root / "model.onnx").unlink(), "MODEL_FILE_MISSING"),
    (lambda root: (root / "model.onnx").write_bytes(b"onnx-but-longer"), "MODEL_FILE_SIZE_MISMATCH"),
    (lambda root: (root / "model.onnx").write_bytes(b"ONNX"), "MODEL_FILE_DIGEST_MISMATCH"),
])
def test_any_drift_from_the_pin_refuses(tmp_path: Path, mutate: object, code: str) -> None:
    pin, root = _pin(tmp_path, {"model.onnx": b"onnx", "tokenizer.json": b"{}"})
    mutate(root)  # type: ignore[operator]
    assert verify_model_dir(pin, root) == code


def test_a_missing_directory_refuses(tmp_path: Path) -> None:
    pin, _ = _pin(tmp_path, {"model.onnx": b"onnx"})
    assert verify_model_dir(pin, tmp_path / "absent") == "MODEL_DIR_MISSING"


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs a privilege on Windows")
def test_a_symlinked_model_file_refuses(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, {"model.onnx": b"onnx"})
    outside = tmp_path / "elsewhere.onnx"
    outside.write_bytes(b"onnx")
    (root / "model.onnx").unlink()
    (root / "model.onnx").symlink_to(outside)
    assert verify_model_dir(pin, root) == "MODEL_FILE_SYMLINK"


@pytest.mark.parametrize("path", ["../escape.onnx", "/abs.onnx", "a/../../b.onnx", "C:/x.onnx", "a\\b.onnx", ""])
def test_a_file_path_outside_the_model_directory_is_not_a_valid_pin(path: str) -> None:
    with pytest.raises(ValueError):
        ModelFile(path=path, sha256="0" * 64, size=1)


@pytest.mark.parametrize("license_spdx", ["CC-BY-NC-4.0", "LicenseRef-Model-Specific", "unknown", ""])
def test_licenses_outside_the_allowlist_are_not_pinnable(tmp_path: Path, license_spdx: str) -> None:
    with pytest.raises(ValueError):
        _pin(tmp_path, {"model.onnx": b"onnx"}, license_spdx=license_spdx)


def test_an_embedding_pin_needs_a_dimension_and_a_cross_encoder_pin_has_none(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _pin(tmp_path, {"model.onnx": b"onnx"}, dimension=None)
    with pytest.raises(ValueError):
        _pin(tmp_path, {"model.onnx": b"onnx"}, role=ModelRole.CROSS_ENCODER, dimension=4)


@pytest.mark.parametrize("field", [{"max_batch": 0}, {"max_batch": 4096}, {"timeout_seconds": 0.0},
                                   {"timeout_seconds": 10_000.0}, {"threads": 0}, {"max_input_chars": 10},
                                   {"revision": "main"}, {"files": ()}])
def test_resource_limits_and_identity_are_bounded(tmp_path: Path, field: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _pin(tmp_path, {"model.onnx": b"onnx"}, **field)


def test_the_pin_digest_changes_with_any_field(tmp_path: Path) -> None:
    pin, _ = _pin(tmp_path, {"model.onnx": b"onnx"})
    assert pin.digest != pin.model_copy(update={"max_batch": 4}).digest
    assert len(pin.digest) == 64


def test_builtin_pins_are_exact_revisions_with_permissive_licenses() -> None:
    assert set(BUILTIN_PINS) == {"bge-small-en-v1.5-onnx-q", "ms-marco-minilm-l6-v2-onnx"}
    for pin in BUILTIN_PINS.values():
        assert len(pin.revision) == 40 and pin.license_spdx in {"MIT", "Apache-2.0"}
        assert all(len(item.sha256) == 64 and item.size > 0 for item in pin.files)
    assert load_pin("bge-small-en-v1.5-onnx-q").dimension == 384
    with pytest.raises(ModelPinError) as excinfo:
        load_pin("jina-reranker-v2-base-multilingual")
    assert excinfo.value.code == "MODEL_PIN_UNKNOWN"
