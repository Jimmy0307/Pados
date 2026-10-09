"""SBOM structure, purls, component counts and determinism (offline)."""
import json
import re

import pytest

from pados.release.sbom import (
    SbomInputError,
    build_cyclonedx,
    npm_components,
    npm_purl,
    parse_requirements_lock,
    pypi_purl,
    python_components,
    render,
    sbom_inputs,
)

from .support import ROOT, TIMESTAMP, fixed_license, fixed_requires, sbom_documents

PY_LOCK = (ROOT / "requirements.lock").read_text(encoding="utf-8-sig")
NPM_LOCK = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
PY_COUNT = len([line for line in PY_LOCK.splitlines() if "==" in line])
NPM_COUNT = len([key for key in NPM_LOCK["packages"] if key.startswith("node_modules/")])


def test_cyclonedx_required_fields() -> None:
    cyclonedx, _ = sbom_documents()
    assert cyclonedx["bomFormat"] == "CycloneDX"
    assert cyclonedx["specVersion"] == "1.5"
    assert cyclonedx["version"] == 1
    assert re.fullmatch(r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                        cyclonedx["serialNumber"])
    assert cyclonedx["metadata"]["timestamp"] == TIMESTAMP
    root = cyclonedx["metadata"]["component"]
    assert root["name"] == "pados" and root["version"] == "0.1.0a1" and root["purl"] == "pkg:pypi/pados@0.1.0a1"
    assert root["licenses"] == [{"license": {"id": "AGPL-3.0-only"}}]
    for component in cyclonedx["components"]:
        assert {"type", "bom-ref", "name", "version", "purl"} <= set(component)
        assert component["type"] == "library"
    refs = [c["bom-ref"] for c in cyclonedx["components"]]
    assert len(refs) == len(set(refs))
    known = set(refs) | {root["bom-ref"]}
    for dependency in cyclonedx["dependencies"]:
        assert dependency["ref"] in known and set(dependency["dependsOn"]) <= known


def test_component_count_equals_lock_entries_and_purls() -> None:
    cyclonedx, spdx = sbom_documents()
    purls = [c["purl"] for c in cyclonedx["components"]]
    pypi = [p for p in purls if p.startswith("pkg:pypi/")]
    npm = [p for p in purls if p.startswith("pkg:npm/")]
    assert len(pypi) == PY_COUNT and len(npm) == NPM_COUNT
    assert len(cyclonedx["components"]) == PY_COUNT + NPM_COUNT
    assert len(spdx["packages"]) == PY_COUNT + NPM_COUNT + 1
    assert "pkg:pypi/sqlalchemy@2.0.51" in pypi and "pkg:pypi/pydantic-core@2.46.4" in pypi
    assert any(p.startswith("pkg:npm/%40babel/core@") for p in npm)
    assert any(p.startswith("pkg:npm/react@") for p in npm)


def test_npm_integrity_becomes_hex_hash_and_scopes() -> None:
    cyclonedx, _ = sbom_documents()
    react = next(c for c in cyclonedx["components"] if c["name"] == "react")
    assert react["scope"] == "required"
    assert react["hashes"][0]["alg"] == "SHA-512" and re.fullmatch(r"[0-9a-f]{128}", react["hashes"][0]["content"])
    vite = next(c for c in cyclonedx["components"] if c["name"] == "vite")
    assert vite["scope"] == "excluded"
    root_deps = cyclonedx["dependencies"][0]["dependsOn"]
    assert react["bom-ref"] in root_deps and "pkg:pypi/fastapi@0.139.0" in root_deps


def test_spdx_structure() -> None:
    _, spdx = sbom_documents()
    assert spdx["spdxVersion"] == "SPDX-2.3" and spdx["dataLicense"] == "CC0-1.0"
    assert spdx["SPDXID"] == "SPDXRef-DOCUMENT"
    assert spdx["creationInfo"]["created"] == TIMESTAMP
    ids = [p["SPDXID"] for p in spdx["packages"]]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"SPDXRef-[A-Za-z0-9.-]+", i) for i in ids)
    assert spdx["documentDescribes"] == [ids[0]]
    for package in spdx["packages"]:
        assert package["externalRefs"][0]["referenceType"] == "purl"


def test_output_is_deterministic_and_serial_is_timestamp_independent() -> None:
    first, second = sbom_documents(), sbom_documents()
    assert render(first[0]) == render(second[0]) and render(first[1]) == render(second[1])
    inputs = sbom_inputs(project={"name": "pados", "version": "0.1.0a1", "license": "AGPL-3.0-only"},
                         requirements_lock=PY_LOCK, package_lock=NPM_LOCK,
                         license_lookup=fixed_license, requires_lookup=fixed_requires)
    later = build_cyclonedx(inputs, timestamp="2030-01-01T00:00:00Z")
    earlier = build_cyclonedx(inputs, timestamp=TIMESTAMP)
    assert later["serialNumber"] == earlier["serialNumber"]
    changed = sbom_inputs(project={"name": "pados", "version": "0.1.0a2"}, requirements_lock=PY_LOCK,
                          package_lock=NPM_LOCK, license_lookup=fixed_license, requires_lookup=fixed_requires)
    assert build_cyclonedx(changed, timestamp=TIMESTAMP)["serialNumber"] != earlier["serialNumber"]


def test_purl_encoding() -> None:
    assert pypi_purl("Typing_Extensions", "4.16.0") == "pkg:pypi/typing-extensions@4.16.0"
    assert pypi_purl("pkg", "1.0+local") == "pkg:pypi/pkg@1.0%2Blocal"
    assert npm_purl("@types/react", "19.1.0") == "pkg:npm/%40types/react@19.1.0"


def test_runtime_scope_follows_requested_extras() -> None:
    lock = "fastapi==1\npsycopg==3\npsycopg-binary==3\npytest==9\n"
    graph = {"fastapi": [], "psycopg": ['psycopg-binary==3; extra == "binary"', 'pool; extra == "pool"'],
             "psycopg-binary": [], "pytest": []}
    components = python_components(lock, runtime_roots=["fastapi>=1", "psycopg[binary]>=3"],
                                   license_lookup=lambda _: None, requires_lookup=graph.get)
    scopes = {c.name: c.scope for c in components}
    assert scopes == {"fastapi": "required", "psycopg": "required", "psycopg-binary": "required",
                      "pytest": "excluded"}


@pytest.mark.parametrize("text,code", [
    ("-e .\n", "UNSUPPORTED_LOCK_LINE:1"),
    ("pkg>=1\n", "UNSUPPORTED_LOCK_LINE:1"),
    ("a==1\nA==2\n", "DUPLICATE_LOCK_ENTRY:2"),
])
def test_lock_parser_fails_closed(text: str, code: str) -> None:
    with pytest.raises(SbomInputError, match=code):
        parse_requirements_lock(text)


def test_lock_parser_accepts_bom_comments_and_markers() -> None:
    assert parse_requirements_lock("﻿# c\nA==1\ncolorama==0.4.6 ; sys_platform == 'win32'\n") == [
        ("A", "1"), ("colorama", "0.4.6")]


def test_npm_lock_rejects_unsupported_entries() -> None:
    with pytest.raises(SbomInputError, match="UNSUPPORTED_PACKAGE_LOCK_VERSION"):
        npm_components({"lockfileVersion": 1, "packages": {}})
    with pytest.raises(SbomInputError, match="UNSUPPORTED_PACKAGE_LOCK_ENTRY"):
        npm_components({"lockfileVersion": 3, "packages": {"node_modules/x": {"link": True}}})
    with pytest.raises(SbomInputError, match="WITHOUT_VERSION"):
        npm_components({"lockfileVersion": 3, "packages": {"node_modules/x": {}}})


def test_timestamp_must_be_explicit_utc() -> None:
    inputs = sbom_inputs(project={"name": "pados", "version": "1"}, requirements_lock="a==1\n",
                         package_lock={"lockfileVersion": 3, "packages": {}},
                         license_lookup=fixed_license, requires_lookup=fixed_requires)
    with pytest.raises(SbomInputError, match="TIMESTAMP"):
        build_cyclonedx(inputs, timestamp="2026-10-02 00:00:00")


@pytest.mark.parametrize("license_field", ["AGPL-3.0-only", {"text": "AGPL-3.0-only"}],
                         ids=["pep639-expression", "legacy-table"])
def test_root_license_accepts_pep639_and_legacy_forms(license_field: object) -> None:
    inputs = sbom_inputs(project={"name": "pados", "version": "0.1.0a1", "license": license_field},
                         requirements_lock=PY_LOCK, package_lock=NPM_LOCK,
                         license_lookup=fixed_license, requires_lookup=fixed_requires)
    assert inputs.root.license == "AGPL-3.0-only"
    assert build_cyclonedx(inputs, timestamp=TIMESTAMP)["metadata"]["component"]["licenses"] == [
        {"license": {"id": "AGPL-3.0-only"}}]
