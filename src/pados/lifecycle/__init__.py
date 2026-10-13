# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS data lifecycle: retention classes, storage categories and data minimization.

Lifecycle is operational policy, not authority. Nothing in this package commits
semantic state, and nothing here deletes an authoritative (R3) record: R3 keeps
its metadata forever while its raw payload may be purged
(``METADATA_RETAINED_PAYLOAD_PURGED``).
"""
