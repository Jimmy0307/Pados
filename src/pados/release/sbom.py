# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic SBOM generation (CycloneDX 1.5 JSON, SPDX 2.3 JSON).

Inputs are the lock files already in the repository (``requirements.lock``,
``web/package-lock.json``) and ``pyproject.toml``; nothing is fetched from the
network. Licenses and the Python dependency graph come from installed package
metadata when available (injectable lookups). Output is deterministic: entries
are sorted, the serial number / document namespace derive from a content hash
that excludes the timestamp, and the timestamp is an explicit input.

An SBOM is inventory evidence. It is not trust, not approval and not PADOS
authority: BUILD SUCCESS != TRUST.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pados.lifecycle.minimization import assert_no_credentials
from pados.versions import PRODUCT_VERSION

CYCLONEDX_SPEC_VERSION = "1.5"
SPDX_VERSION = "SPDX-2.3"
TOOL_NAME = "pados-sbom"
SPDX_NAMESPACE_BASE = "https://pados.invalid/spdx"

# SPDX identifiers emitted as CycloneDX license ``id``; anything else is a ``name``.
_KNOWN_SPDX_IDS = frozenset({
    "0BSD", "AGPL-3.0-only", "Apache-2.0", "BlueOak-1.0.0", "BSD-2-Clause", "BSD-3-Clause", "CC-BY-4.0", "CC0-1.0", "ISC",
    "LGPL-2.1-only", "LGPL-3.0-only", "MIT", "MIT-0", "MPL-2.0", "PSF-2.0", "Python-2.0", "Unlicense", "Zlib",
})
_SPDX_EXPRESSION = re.compile(r"[A-Za-z0-9.+-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+-]+)*|\([A-Za-z0-9.+ ()-]+\)")
_LOCK_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.!+_-]+)(?:\s*;\s*(.+))?")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_INTEGRITY_ALGS = {"sha512": "SHA-512", "sha384": "SHA-384", "sha256": "SHA-256", "sha1": "SHA-1"}
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SbomInputError(ValueError):
    pass


LicenseLookup = Callable[[str], str | None]
RequiresLookup = Callable[[str], Sequence[str] | None]


@dataclass(frozen=True)
class Component:
    ecosystem: str  # "pypi" | "npm"
    name: str
    version: str
    purl: str
    bom_ref: str
    license: str | None = None
    hashes: tuple[tuple[str, str], ...] = ()
    download: str | None = None
    scope: str | None = None  # CycloneDX scope
    properties: tuple[tuple[str, str], ...] = ()
    depends_on: tuple[str, ...] = field(default=(), compare=False)


def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{normalize_pypi(name)}@{quote(version, safe='.-_~!')}"


def npm_purl(name: str, version: str) -> str:
    if name.startswith("@"):
        scope, _, base = name.partition("/")
        return f"pkg:npm/{quote(scope, safe='')}/{quote(base, safe='.-_~')}@{quote(version, safe='.-_~')}"
    return f"pkg:npm/{quote(name, safe='.-_~')}@{quote(version, safe='.-_~')}"


def parse_requirements_lock(text: str) -> list[tuple[str, str]]:
    """``name==version`` lines only; anything else (editable, URL, options) fails closed."""
    pins: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        if match is None:
            raise SbomInputError(f"UNSUPPORTED_LOCK_LINE:{number}")
        key = normalize_pypi(match.group(1))
        if key in seen:
            raise SbomInputError(f"DUPLICATE_LOCK_ENTRY:{number}")
        seen.add(key)
        pins.append((match.group(1), match.group(2)))
    return pins


def installed_license(name: str) -> str | None:
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return None
    expression = meta.get("License-Expression")
    if expression:
        return str(expression)
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(sorted(classifiers))
    lines = str(meta.get("License") or "").strip().splitlines()
    return lines[0][:80].strip() or None if lines else None


def installed_requires(name: str) -> Sequence[str] | None:
    try:
        return metadata.requires(name) or []
    except metadata.PackageNotFoundError:
        return None


def _requirement_name(requirement: str) -> str | None:
    if re.search(r"extra\s*==", requirement):
        return None
    match = _REQUIREMENT_NAME.match(requirement.strip())
    return normalize_pypi(match.group(0)) if match else None


def _requirement_extras(requirement: str) -> frozenset[str]:
    match = re.match(r"\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*\[([^\]]*)\]", requirement)
    return frozenset(e.strip().lower() for e in match.group(1).split(",") if e.strip()) if match else frozenset()


def runtime_closure(roots: Sequence[str], requires: RequiresLookup) -> set[str]:
    """Normalized names reachable from ``roots`` following requested extras only.

    A package without installed metadata is treated as a leaf.
    """
    seen: set[tuple[str, frozenset[str]]] = set()
    stack = [(name, _requirement_extras(r)) for r in roots if (name := _requirement_name(r))]
    while stack:
        name, extras = stack.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        for requirement in requires(name) or []:
            extra = re.search(r"extra\s*==\s*[\"']([^\"']+)[\"']", requirement)
            if extra is not None and extra.group(1).lower() not in extras:
                continue
            child = _requirement_name(requirement.split(";", 1)[0])
            if child:
                stack.append((child, _requirement_extras(requirement)))
    return {name for name, _ in seen}


def python_components(
    lock_text: str,
    *,
    runtime_roots: Sequence[str] = (),
    license_lookup: LicenseLookup = installed_license,
    requires_lookup: RequiresLookup = installed_requires,
) -> list[Component]:
    pins = parse_requirements_lock(lock_text)
    locked = {normalize_pypi(name): (name, version) for name, version in pins}
    closure = runtime_closure(runtime_roots, requires_lookup)
    components = []
    for key in sorted(locked):
        name, version = locked[key]
        purl = pypi_purl(name, version)
        requires = requires_lookup(name)
        depends = sorted({pypi_purl(*locked[child]) for child in (_requirement_name(r) for r in requires or [])
                          if child and child in locked and child != key})
        runtime = key in closure
        properties = [("pados:scope", "RUNTIME" if runtime else "DEV_OR_BUILD_ONLY")]
        if requires is None:
            properties.append(("pados:metadata", "NOT_INSTALLED"))
        components.append(Component(
            ecosystem="pypi", name=name, version=version, purl=purl, bom_ref=purl, license=license_lookup(name),
            scope="required" if runtime else "excluded", properties=tuple(properties), depends_on=tuple(depends),
        ))
    return components


def _integrity_hashes(integrity: str) -> tuple[tuple[str, str], ...]:
    hashes = []
    for token in integrity.split():
        alg, _, encoded = token.partition("-")
        if alg not in _INTEGRITY_ALGS:
            raise SbomInputError("UNSUPPORTED_INTEGRITY_ALGORITHM")
        hashes.append((_INTEGRITY_ALGS[alg], base64.b64decode(encoded, validate=True).hex()))
    return tuple(sorted(hashes))


def _resolve_npm(lock_key: str, dependency: str, packages: Mapping[str, Any]) -> str | None:
    """Node module resolution over package-lock paths (nearest node_modules wins)."""
    base = lock_key
    while True:
        candidate = f"{base}/node_modules/{dependency}" if base else f"node_modules/{dependency}"
        if candidate in packages:
            return candidate
        if not base:
            return None
        index = base.rfind("/node_modules/")
        base = base[:index] if index >= 0 else ""


def npm_components(package_lock: Mapping[str, Any]) -> tuple[list[Component], tuple[str, ...]]:
    """Components for every ``node_modules/`` entry plus the bom-refs of the root's runtime deps."""
    if package_lock.get("lockfileVersion") not in {2, 3}:
        raise SbomInputError("UNSUPPORTED_PACKAGE_LOCK_VERSION")
    packages: Mapping[str, Any] = package_lock.get("packages") or {}
    entries: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
    for key in sorted(packages):
        if not key:
            continue
        entry = packages[key]
        if not key.startswith("node_modules/") or not isinstance(entry, Mapping) or entry.get("link"):
            raise SbomInputError("UNSUPPORTED_PACKAGE_LOCK_ENTRY")
        name = str(entry.get("name") or key.rsplit("node_modules/", 1)[1])
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            raise SbomInputError("PACKAGE_LOCK_ENTRY_WITHOUT_VERSION")
        entries[key] = (name, version, entry)
    purl_counts: dict[str, int] = {}
    for name, version, _ in entries.values():
        purl = npm_purl(name, version)
        purl_counts[purl] = purl_counts.get(purl, 0) + 1
    refs = {}
    for key, (name, version, _) in entries.items():
        purl = npm_purl(name, version)
        refs[key] = purl if purl_counts[purl] == 1 else f"{purl}#{key}"
    components = []
    for key, (name, version, entry) in entries.items():
        wanted = {**(entry.get("dependencies") or {}), **(entry.get("optionalDependencies") or {})}
        depends = sorted({refs[path] for dep in wanted if (path := _resolve_npm(key, dep, packages))})
        dev = bool(entry.get("dev") or entry.get("devOptional"))
        properties = [("pados:npm:lock_path", key), ("pados:scope", "DEV_OR_BUILD_ONLY" if dev else "RUNTIME")]
        components.append(Component(
            ecosystem="npm", name=name, version=version, purl=npm_purl(name, version), bom_ref=refs[key],
            license=str(entry["license"]) if entry.get("license") else None,
            hashes=_integrity_hashes(str(entry["integrity"])) if entry.get("integrity") else (),
            download=str(entry["resolved"]) if entry.get("resolved") else None,
            scope="excluded" if dev else "required", properties=tuple(properties), depends_on=tuple(depends),
        ))
    root = packages.get("") or {}
    direct = sorted({refs[path] for dep in (root.get("dependencies") or {})
                     if (path := _resolve_npm("", dep, packages))})
    return components, tuple(direct)


def _cdx_license(value: str) -> dict[str, Any]:
    if value in _KNOWN_SPDX_IDS:
        return {"license": {"id": value}}
    if (" OR " in value or " AND " in value) and _SPDX_EXPRESSION.fullmatch(value):
        return {"expression": value}
    return {"license": {"name": value}}


def _cdx_component(component: Component, component_type: str = "library") -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": component_type, "bom-ref": component.bom_ref, "name": component.name,
        "version": component.version, "purl": component.purl,
    }
    if component.license:
        out["licenses"] = [_cdx_license(component.license)]
    if component.hashes:
        out["hashes"] = [{"alg": alg, "content": content} for alg, content in component.hashes]
    if component.download:
        out["externalReferences"] = [{"type": "distribution", "url": component.download}]
    if component.scope:
        out["scope"] = component.scope
    if component.properties:
        out["properties"] = [{"name": k, "value": v} for k, v in sorted(component.properties)]
    return out


def content_uuid(document: Mapping[str, Any]) -> str:
    """UUID (version 5 layout) from the SHA-256 of the canonical document."""
    digest = hashlib.sha256(canonical_bytes(document)).digest()
    return str(uuid.UUID(bytes=digest[:16], version=5))


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def render(document: Mapping[str, Any]) -> bytes:
    """Stable on-disk form (sorted keys, two-space indent, trailing newline)."""
    return (json.dumps(document, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def _check_timestamp(timestamp: str) -> None:
    if not _TIMESTAMP.fullmatch(timestamp):
        raise SbomInputError("TIMESTAMP_MUST_BE_UTC_ISO8601")
    datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SbomInputs:
    root: Component
    components: tuple[Component, ...]
    root_depends_on: tuple[str, ...]
    source_digests: tuple[tuple[str, str], ...]  # (source file, sha256)


def build_cyclonedx(inputs: SbomInputs, *, timestamp: str) -> dict[str, Any]:
    _check_timestamp(timestamp)
    refs = [c.bom_ref for c in inputs.components]
    if len(set(refs)) != len(refs) or inputs.root.bom_ref in refs:
        raise SbomInputError("DUPLICATE_BOM_REF")
    components = sorted(inputs.components, key=lambda c: (c.ecosystem, c.name.lower(), c.version, c.bom_ref))
    dependencies = [{"ref": inputs.root.bom_ref, "dependsOn": sorted(inputs.root_depends_on)}]
    dependencies += [{"ref": c.bom_ref, "dependsOn": list(c.depends_on)} for c in components]
    properties = [{"name": f"pados:source_sha256:{name}", "value": digest}
                  for name, digest in sorted(inputs.source_digests)]
    document: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "tools": {"components": [{"type": "application", "name": TOOL_NAME, "version": PRODUCT_VERSION}]},
            "component": _cdx_component(inputs.root, "application"),
            "properties": properties,
        },
        "components": [_cdx_component(c) for c in components],
        "dependencies": dependencies,
    }
    document["serialNumber"] = f"urn:uuid:{content_uuid(document)}"
    document["metadata"]["timestamp"] = timestamp
    return document


def _spdx_id(component: Component) -> str:
    raw = f"{component.ecosystem}-{component.bom_ref.split('#', 1)[-1] if '#' in component.bom_ref else component.name}"
    return "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]+", "-", f"{raw}-{component.version}").strip("-")


def _spdx_license(value: str | None) -> str:
    if value and (value in _KNOWN_SPDX_IDS or ((" OR " in value or " AND " in value)
                                               and _SPDX_EXPRESSION.fullmatch(value))):
        return value
    return "NOASSERTION"


def _spdx_package(component: Component) -> dict[str, Any]:
    package: dict[str, Any] = {
        "SPDXID": _spdx_id(component), "name": component.name, "versionInfo": component.version,
        "downloadLocation": component.download or "NOASSERTION", "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION", "licenseDeclared": _spdx_license(component.license),
        "copyrightText": "NOASSERTION",
        "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                          "referenceLocator": component.purl}],
    }
    if component.hashes:
        package["checksums"] = [{"algorithm": alg.replace("-", ""), "checksumValue": content}
                                for alg, content in component.hashes]
    return package


def build_spdx(inputs: SbomInputs, *, timestamp: str) -> dict[str, Any]:
    _check_timestamp(timestamp)
    components = sorted(inputs.components, key=lambda c: (c.ecosystem, c.name.lower(), c.version, c.bom_ref))
    ids = [_spdx_id(c) for c in components]
    root_id = _spdx_id(inputs.root)
    if len(set(ids)) != len(ids) or root_id in ids:
        raise SbomInputError("DUPLICATE_SPDX_ID")
    by_ref = {c.bom_ref: _spdx_id(c) for c in components}
    relationships = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                      "relatedSpdxElement": root_id}]
    relationships += [{"spdxElementId": root_id, "relationshipType": "DEPENDS_ON", "relatedSpdxElement": i}
                      for i in ids]
    relationships += [{"spdxElementId": by_ref[c.bom_ref], "relationshipType": "DEPENDS_ON",
                       "relatedSpdxElement": by_ref[d]} for c in components for d in c.depends_on if d in by_ref]
    document: dict[str, Any] = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{inputs.root.name}-{inputs.root.version}",
        "documentDescribes": [root_id],
        "packages": [_spdx_package(inputs.root)] + [_spdx_package(c) for c in components],
        "relationships": relationships,
    }
    document["documentNamespace"] = f"{SPDX_NAMESPACE_BASE}/{inputs.root.name}-{inputs.root.version}-" \
                                    f"{content_uuid(document)}"
    document["creationInfo"] = {"created": timestamp, "creators": [f"Tool: {TOOL_NAME}-{PRODUCT_VERSION}"]}
    return document


def _project_license(license_field: object) -> str | None:
    """The root license: a PEP 639 SPDX expression string, or the legacy ``{text = ...}`` table."""
    if isinstance(license_field, str):
        return license_field.strip() or None
    if isinstance(license_field, Mapping) and license_field.get("text"):
        return str(license_field["text"])
    return None


def sbom_inputs(
    *,
    project: Mapping[str, Any],
    requirements_lock: str,
    package_lock: Mapping[str, Any],
    source_digests: Sequence[tuple[str, str]] = (),
    license_lookup: LicenseLookup = installed_license,
    requires_lookup: RequiresLookup = installed_requires,
) -> SbomInputs:
    """Assemble inputs from parsed ``pyproject.toml`` [project], the lock texts and lookups."""
    name, version = str(project["name"]), str(project["version"])
    root_license = _project_license(project.get("license"))
    runtime_roots = [str(r) for r in project.get("dependencies", [])]
    python = python_components(requirements_lock, runtime_roots=runtime_roots,
                               license_lookup=license_lookup, requires_lookup=requires_lookup)
    npm, npm_direct = npm_components(package_lock)
    locked = {normalize_pypi(c.name): c.bom_ref for c in python}
    direct_python = {locked[n] for n in (_requirement_name(r) for r in runtime_roots) if n and n in locked}
    root_purl = pypi_purl(name, version)
    root = Component("pypi", name, version, root_purl, root_purl, license=root_license)
    return SbomInputs(root=root, components=tuple(python + npm),
                      root_depends_on=tuple(sorted(direct_python | set(npm_direct))),
                      source_digests=tuple(source_digests))


def generate_for_project(
    project_root: Path,
    *,
    timestamp: str,
    license_lookup: LicenseLookup = installed_license,
    requires_lookup: RequiresLookup = installed_requires,
    build_environment: bytes | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(CycloneDX, SPDX) documents for the repository at ``project_root`` (offline).

    ``build_environment`` is the build job's actual ``pip freeze`` inventory; its
    digest is bound into the SBOM metadata as an additional evidence input.
    """
    sources = {name: (project_root / name).read_bytes()
               for name in ("pyproject.toml", "requirements.lock", "web/package-lock.json")}
    if build_environment is not None:
        sources["build-environment.txt"] = build_environment
    inputs = sbom_inputs(
        project=tomllib.loads(sources["pyproject.toml"].decode("utf-8"))["project"],
        requirements_lock=sources["requirements.lock"].decode("utf-8-sig"),
        package_lock=json.loads(sources["web/package-lock.json"].decode("utf-8")),
        source_digests=[(name, hashlib.sha256(data).hexdigest()) for name, data in sources.items()],
        license_lookup=license_lookup, requires_lookup=requires_lookup,
    )
    cyclonedx, spdx = build_cyclonedx(inputs, timestamp=timestamp), build_spdx(inputs, timestamp=timestamp)
    assert_no_credentials(cyclonedx)
    assert_no_credentials(spdx)
    return cyclonedx, spdx
