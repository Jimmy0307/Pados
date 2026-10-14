# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS-owned binding of learning materials to a project (P7.2 hardening).

A ``material_ref`` on its own is only syntactically valid; it carries no proof that the material belongs to
the requesting project. This resolver binds each referenced material to an operator registration before any
external learning runtime is called: the material must be registered to ``request.project_id`` and its
content digest must match. The OpenMAIC adapter never judges project ownership -- it only ever receives
material ids PADOS already verified here.

This is a thin, provider-neutral binding type. It activates nothing, grants nothing, and does not touch any
Core model; it only answers "may these materials be used for this project's learning request?".
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pados.capability_loop.learning_contracts import LearningRequest


class MaterialResolutionCode(StrEnum):
    RESOLVED = "RESOLVED"
    UNKNOWN_MATERIAL = "UNKNOWN_MATERIAL"  # no operator registration for this material ref
    REGISTRATION_IDENTITY_MISMATCH = "REGISTRATION_IDENTITY_MISMATCH"  # key / ref / request ref disagree
    CROSS_PROJECT = "CROSS_PROJECT"  # registered to a different project than the request
    DIGEST_MISMATCH = "DIGEST_MISMATCH"  # the request's content digest disagrees with the registration


# The policy code recorded on a receipt when a material fails to bind.
_POLICY_CODE = {
    MaterialResolutionCode.UNKNOWN_MATERIAL: "MATERIAL_UNKNOWN",
    MaterialResolutionCode.REGISTRATION_IDENTITY_MISMATCH: "MATERIAL_IDENTITY_MISMATCH",
    MaterialResolutionCode.CROSS_PROJECT: "MATERIAL_CROSS_PROJECT",
    MaterialResolutionCode.DIGEST_MISMATCH: "MATERIAL_DIGEST_MISMATCH",
}


def material_policy_code(code: MaterialResolutionCode) -> str:
    return _POLICY_CODE[code]


@dataclass(frozen=True)
class MaterialRegistration:
    """An operator's record that a material belongs to a project with a known content digest."""

    material_ref: str
    project_id: str
    content_digest: str


@dataclass(frozen=True)
class MaterialResolution:
    code: MaterialResolutionCode
    material_refs: tuple[str, ...] = ()
    offending_ref: str | None = None


def resolve_materials(request: LearningRequest,
                      registrations: Mapping[str, MaterialRegistration]) -> MaterialResolution:
    """Bind every material in ``request`` to ``request.project_id``. First failure wins, fail closed.

    Returns ``RESOLVED`` with the verified material refs (empty when the request has no materials), or the
    first binding failure with the offending ref. Nothing is sent to a provider on a non-RESOLVED result.
    """
    verified: list[str] = []
    for material in request.materials:
        registration = registrations.get(material.material_ref)
        if registration is None:
            return MaterialResolution(MaterialResolutionCode.UNKNOWN_MATERIAL, offending_ref=material.material_ref)
        # The mapping key, the registration's declared ref and the requested ref must be one identity; a
        # mis-keyed registration must never be used in place of the requested material.
        if registration.material_ref != material.material_ref:
            return MaterialResolution(MaterialResolutionCode.REGISTRATION_IDENTITY_MISMATCH,
                                      offending_ref=material.material_ref)
        if registration.project_id != request.project_id:
            return MaterialResolution(MaterialResolutionCode.CROSS_PROJECT, offending_ref=material.material_ref)
        if registration.content_digest != material.content_digest:
            return MaterialResolution(MaterialResolutionCode.DIGEST_MISMATCH, offending_ref=material.material_ref)
        verified.append(material.material_ref)
    return MaterialResolution(MaterialResolutionCode.RESOLVED, material_refs=tuple(verified))
