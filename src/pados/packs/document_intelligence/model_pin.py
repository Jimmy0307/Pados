# SPDX-License-Identifier: AGPL-3.0-only
"""Operator model pins for the local embedding and cross-encoder engines.

A pin is the audited identity of a model artifact: the source repository and exact 40-hex revision, the
upstream license (an allowlist of permissive SPDX ids; non-commercial and unknown licenses are not pinnable),
and the SHA-256 and size of every file the engine loads. It also fixes the resource bounds the engine runs
under (batch size, input length, wall-clock timeout, threads, RAM class).

PADOS never downloads a model. The operator fetches the pinned files once
(``scripts/models/fetch_pinned_models.py``) into a directory of their choice; ``verify_model_dir`` then reads
that directory and refuses, with a closed code, any missing, resized, altered, symlinked or escaping file. A
tag or a model name alone never satisfies a pin.
"""
from __future__ import annotations

import hashlib
import re
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.execution.context import canonical_hash

PIN_SCHEMA: Final = "pados.document-intelligence.model-pin.v1"
PERMISSIVE_LICENSES: Final = frozenset({"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause"})
MAX_FILES = 16
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024
_SHA256 = r"^[0-9a-f]{64}$"
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelPinError(ValueError):
    """Closed-code pin refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelRole(StrEnum):
    EMBEDDING = "EMBEDDING"
    CROSS_ENCODER = "CROSS_ENCODER"


class ModelFile(BaseModel):
    model_config = _FROZEN

    path: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=_SHA256)
    size: int = Field(ge=1, le=MAX_FILE_BYTES)

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        pure = PurePosixPath(value)
        if ("\\" in value or ":" in value or pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts)
                or not re.fullmatch(r"[A-Za-z0-9._/-]+", value)):
            raise ValueError("MODEL_FILE_PATH_INVALID")
        return value


class ModelPin(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["pados.document-intelligence.model-pin.v1"] = Field(default=PIN_SCHEMA, alias="schema")
    pin_id: str = Field(pattern=r"^[a-z][a-z0-9.-]{2,63}$")
    role: ModelRole
    engine: Literal["fastembed"]
    model_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,127}$")  # the engine's registry name
    source_repo: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    license_spdx: str
    license_source: str = Field(default="", max_length=128)  # where the license was read (upstream card)
    files: tuple[ModelFile, ...] = Field(min_length=1, max_length=MAX_FILES)
    dimension: int | None = Field(default=None, ge=2, le=4096)
    max_batch: int = Field(ge=1, le=256)
    max_input_chars: int = Field(ge=64, le=32_768)
    timeout_seconds: float = Field(gt=0.0, le=600.0)
    threads: int = Field(ge=1, le=16)
    max_ram_mb: int = Field(ge=128, le=65_536)

    @field_validator("license_spdx")
    @classmethod
    def _permissive(cls, value: str) -> str:
        if value not in PERMISSIVE_LICENSES:
            raise ValueError("MODEL_LICENSE_NOT_ALLOWED")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> ModelPin:
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("MODEL_PIN_FILES_DUPLICATE")
        if (self.role is ModelRole.EMBEDDING) != (self.dimension is not None):
            raise ValueError("MODEL_PIN_DIMENSION_INVALID")
        return self

    @property
    def digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json", by_alias=True))


def _file_digest(path: Path, limit: int) -> tuple[int, str] | None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                return None
            digest.update(chunk)
    return size, digest.hexdigest()


def verify_model_dir(pin: ModelPin, directory: Path) -> str | None:
    """``None`` when ``directory`` holds exactly the pinned bytes for every pinned file, else a closed code."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            return "MODEL_DIR_MISSING"
        base = directory.resolve(strict=True)
        for item in pin.files:
            path = directory
            for part in PurePosixPath(item.path).parts:  # no component below the model directory may be a link
                path = path / part
                if path.is_symlink():
                    return "MODEL_FILE_SYMLINK"
            try:
                info = path.lstat()
            except FileNotFoundError:
                return "MODEL_FILE_MISSING"
            if stat.S_ISLNK(info.st_mode):
                return "MODEL_FILE_SYMLINK"
            if not stat.S_ISREG(info.st_mode) or base not in path.resolve(strict=True).parents:
                return "MODEL_FILE_INVALID"
            if info.st_size != item.size:
                return "MODEL_FILE_SIZE_MISMATCH"
            measured = _file_digest(path, item.size)
            if measured is None or measured[0] != item.size:
                return "MODEL_FILE_SIZE_MISMATCH"
            if measured[1] != item.sha256:
                return "MODEL_FILE_DIGEST_MISMATCH"
    except OSError:
        return "MODEL_DIR_UNREADABLE"
    return None


def _files(*entries: tuple[str, int, str]) -> tuple[ModelFile, ...]:
    return tuple(ModelFile(path=path, size=size, sha256=sha) for path, size, sha in entries)


_TOKENIZER_JSON = ("tokenizer.json", 711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66")
_TOKENIZER_CONFIG = ("tokenizer_config.json", 1242,
                     "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53")

# Recorded by the operator fetch on 2026-11-14; the ONNX digests equal the Hugging Face LFS SHA-256 records.
BUILTIN_PINS: Final[dict[str, ModelPin]] = {pin.pin_id: pin for pin in (
    ModelPin(pin_id="bge-small-en-v1.5-onnx-q", role=ModelRole.EMBEDDING, engine="fastembed",
             model_name="BAAI/bge-small-en-v1.5", source_repo="Qdrant/bge-small-en-v1.5-onnx-Q",
             revision="aa8f8b060edb00e03bfdd08813a2949946c8ba55", license_spdx="MIT",
             license_source="BAAI/bge-small-en-v1.5@5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
             files=_files(("model_optimized.onnx", 66465124,
                           "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"),
                          _TOKENIZER_JSON, _TOKENIZER_CONFIG,
                          ("config.json", 706, "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd"),
                          ("special_tokens_map.json", 695,
                           "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a")),
             dimension=384, max_batch=32, max_input_chars=2000, timeout_seconds=60.0, threads=2, max_ram_mb=1024),
    ModelPin(pin_id="ms-marco-minilm-l6-v2-onnx", role=ModelRole.CROSS_ENCODER, engine="fastembed",
             model_name="Xenova/ms-marco-MiniLM-L-6-v2", source_repo="Xenova/ms-marco-MiniLM-L-6-v2",
             revision="a09144355adeed5f58c8ed011d209bf8ee5a1fec", license_spdx="Apache-2.0",
             license_source="cross-encoder/ms-marco-MiniLM-L-6-v2@233902d25c440f23af6f7d6e94d2946bac0bee0a",
             files=_files(("onnx/model.onnx", 90992115,
                           "c623d0bcb99f4622beb413eaef00cfbe5db20df9f1dd982da4b4f26022881870"),
                          _TOKENIZER_JSON, _TOKENIZER_CONFIG,
                          ("config.json", 824, "d827779a72d27ae68cf878a6fc2e954542663fe21ca515d9f4783fc96be2d37e"),
                          ("special_tokens_map.json", 125,
                           "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3")),
             max_batch=16, max_input_chars=2000, timeout_seconds=60.0, threads=2, max_ram_mb=1024),
)}


def load_pin(pin_id: str) -> ModelPin:
    pin = BUILTIN_PINS.get(pin_id)
    if pin is None:
        raise ModelPinError("MODEL_PIN_UNKNOWN")
    return pin
