"""Disclosure scanner: redacted credential URLs are not secrets; real ones still are."""
from __future__ import annotations

from scripts.audit_consolidation import line_findings

SCHEME = "postgresql+psycopg" + "://"


def test_masked_credential_url_forms_are_not_findings() -> None:
    for secret in ("***", "********", "[REDACTED]", "[REDACTED: credential]", "<redacted>"):
        assert "credential_url" not in line_findings(f"url={SCHEME}pados:{secret}@127.0.0.1:5432/db")


def test_literal_or_partially_masked_credentials_are_still_findings() -> None:
    for secret in ("hunter2value", "ab***", "***x", "pw*"):
        assert "credential_url" in line_findings(f"url={SCHEME}pados:{secret}@127.0.0.1:5432/db")


def test_other_rules_are_unchanged() -> None:
    assert "private_key" in line_findings("-----BEGIN " + "PRIVATE KEY-----")
    assert "credential_token" in line_findings("token ghp_" + "A" * 20)
