"""Deterministic fixtures for the supply-chain tests (no network, no git writes)."""
from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pados.enterprise.secrets import SecretBoundary, SecretValue
from pados.release.approval import ConsumedApprovalLedger, HmacApprovalAuthority, OperatorRegistry
from pados.release.sbom import generate_for_project, render
from pados.release.supply_chain import (
    BuildInvocation,
    ReleaseMetadata,
    SourceIdentity,
    write_release_metadata,
)
from pados.versions import PRODUCT_VERSION

ROOT = Path(__file__).resolve().parents[2]
TIMESTAMP = "2026-10-02T00:00:00Z"
COMMIT = "a" * 40
TREE = "b" * 40
WHEEL = f"pados-{PRODUCT_VERSION}-py3-none-any.whl"
SDIST = f"pados-{PRODUCT_VERSION}.tar.gz"
WEB = f"pados-web-{PRODUCT_VERSION}.tar.gz"
BUILD_ENVIRONMENT = b"build==1.6.1\nsetuptools==84.0.0\nwheel==0.48.0\n"
KEY_REF = "secretref://homebase/deploy-approval-key"
TARGET = "home-base-01"
APPROVER = "op.jimmy"
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def fixed_license(name: str) -> str | None:
    return "MIT" if name != "colorama" else None


def fixed_requires(name: str) -> Sequence[str] | None:
    return [] if name not in {"colorama", "tzdata"} else None


def sbom_documents(build_environment: bytes | None = BUILD_ENVIRONMENT) -> tuple[dict[str, Any], dict[str, Any]]:
    return generate_for_project(ROOT, timestamp=TIMESTAMP, license_lookup=fixed_license,
                                requires_lookup=fixed_requires, build_environment=build_environment)


def build_release_dir(tmp: Path, *, dirty: bool = False, attestation: bool = False) -> tuple[Path, ReleaseMetadata]:
    release = tmp / "release"
    release.mkdir(parents=True)
    (release / WHEEL).write_bytes(b"wheel-bytes")
    (release / SDIST).write_bytes(b"sdist-bytes")
    (release / WEB).write_bytes(b"web-bytes")
    (release / "build-environment.txt").write_bytes(BUILD_ENVIRONMENT)
    cyclonedx, spdx = sbom_documents()
    (release / "sbom.cdx.json").write_bytes(render(cyclonedx))
    (release / "sbom.spdx.json").write_bytes(render(spdx))
    metadata = write_release_metadata(
        ROOT, release, source=SourceIdentity(COMMIT, TREE, dirty, "git+https://example.invalid/pados", None),
        invocation=BuildInvocation(started_on=TIMESTAMP, finished_on=TIMESTAMP), created_at=TIMESTAMP,
    )
    if attestation:
        (release / "attestation.sigstore.json").write_text(json.dumps({"mediaType": "bundle"}), encoding="utf-8")
    return release, metadata


class FixedResolver:
    """In-memory SecretRef resolver standing in for the operator's Home Base secret store."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, ref: str) -> SecretValue | None:
        value = self._values.get(ref)
        return SecretValue(value) if value else None


def make_authority(tmp: Path, *, key: str | None = None, operators: Sequence[str] = (APPROVER,),
                   environ: dict[str, str] | None = None) -> HmacApprovalAuthority:
    registry = OperatorRegistry.from_document({
        "schema": "pados.deploy-operators/v1",
        "operators": [{"operator_id": o, "enabled": True} for o in operators],
    })
    return HmacApprovalAuthority(
        key_ref=KEY_REF, boundary=SecretBoundary(FixedResolver({KEY_REF: key or secrets.token_hex(32)})),
        registry=registry, ledger=ConsumedApprovalLedger(tmp / "consumed-approvals.jsonl"),
        environ={} if environ is None else environ,
    )
