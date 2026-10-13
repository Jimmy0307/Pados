# SPDX-License-Identifier: AGPL-3.0-only
"""High-confidence secret scanner for CI (repository-owned, no third-party action).

The scanner looks for provider token shapes and embedded credentials in the
tracked tree. A finding records only path, line and rule: the matched value is
never copied into a report, log or exception. Known non-secret matches are
classified by an explicit, reviewed allowlist (path glob + rule + reason); an
unclassified finding fails the gate. This complements, and does not replace,
GitHub secret scanning and push protection (repository settings, see
docs/supply_chain/SECURE_CICD_V1.md).
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.lifecycle.minimization import assert_no_credentials

MAX_SCAN_BYTES = 2 * 1024 * 1024


class SecretRule(StrEnum):
    GITHUB_TOKEN = "GITHUB_TOKEN"
    AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
    SLACK_TOKEN = "SLACK_TOKEN"
    LLM_PROVIDER_KEY = "LLM_PROVIDER_KEY"
    GOOGLE_API_KEY = "GOOGLE_API_KEY"
    STRIPE_LIVE_KEY = "STRIPE_LIVE_KEY"
    PRIVATE_KEY_BLOCK = "PRIVATE_KEY_BLOCK"
    URL_EMBEDDED_PASSWORD = "URL_EMBEDDED_PASSWORD"


# The scheme starts at a non-scheme boundary and is bounded, so long alphanumeric runs (e.g. a
# 300 KB single-line fixture) are scanned in linear time instead of quadratic backtracking.
_URL_PASSWORD = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,31}://[^/\s:@'\"<>]+:([^@\s/'\"<>]{3,})@[A-Za-z0-9.\[-]")
_PLACEHOLDER_PASSWORD = re.compile(r"^(\*+|x+|\.{3}|<[^>]*>|password|pass|changeme|redacted|\[redacted\])$",
                                   re.IGNORECASE)

RULES: dict[SecretRule, re.Pattern[str]] = {
    SecretRule.GITHUB_TOKEN: re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    SecretRule.AWS_ACCESS_KEY_ID: re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    SecretRule.SLACK_TOKEN: re.compile(r"(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}"),
    SecretRule.LLM_PROVIDER_KEY: re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"),
    SecretRule.GOOGLE_API_KEY: re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    SecretRule.STRIPE_LIVE_KEY: re.compile(r"(?<![A-Za-z0-9])[sr]k_live_[0-9A-Za-z]{24,}"),
    SecretRule.PRIVATE_KEY_BLOCK: re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"),
}


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    rule: SecretRule
    classification: str  # "ALLOWED" (with reason) or "UNCLASSIFIED"
    reason: str = ""

    def as_record(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "rule": self.rule.value,
                "classification": self.classification, "reason": self.reason}


@dataclass(frozen=True)
class AllowEntry:
    path: str
    rule: SecretRule
    reason: str


class SecretScanError(ValueError):
    pass


def load_allowlist(entries: Sequence[Mapping[str, Any]]) -> tuple[AllowEntry, ...]:
    allowed: list[AllowEntry] = []
    for entry in entries:
        reason = str(entry.get("reason", "")).strip()
        if not reason or not entry.get("path"):
            raise SecretScanError("ALLOWLIST_ENTRY_REQUIRES_PATH_AND_REASON")
        allowed.append(AllowEntry(str(entry["path"]), SecretRule(str(entry["rule"])), reason))
    return tuple(allowed)


def _url_password_lines(text: str) -> Iterable[int]:
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _URL_PASSWORD.finditer(line):
            candidate = match.group(1)
            if _PLACEHOLDER_PASSWORD.fullmatch(candidate) or any(c in candidate for c in "{}$%"):
                continue
            yield number
            break


def scan_text(path: str, text: str) -> list[tuple[int, SecretRule]]:
    hits: list[tuple[int, SecretRule]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                hits.append((number, rule))
    hits.extend((number, SecretRule.URL_EMBEDDED_PASSWORD) for number in _url_password_lines(text))
    return sorted(hits)


def _classify(path: str, rule: SecretRule, allowlist: Sequence[AllowEntry]) -> tuple[str, str]:
    for entry in allowlist:
        if entry.rule is rule and fnmatch.fnmatchcase(path, entry.path):
            return "ALLOWED", entry.reason
    return "UNCLASSIFIED", ""


def scan_files(root: Path, paths: Iterable[str], allowlist: Sequence[AllowEntry]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for relative in sorted(set(paths)):
        full = root / relative
        if full.is_symlink() or not full.is_file() or full.stat().st_size > MAX_SCAN_BYTES:
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line, rule in scan_text(relative, text):
            classification, reason = _classify(relative, rule, allowlist)
            findings.append(SecretFinding(relative, line, rule, classification, reason))
    return findings


def scan_report(findings: Sequence[SecretFinding], *, scanned_files: int) -> dict[str, Any]:
    unclassified = [f.as_record() for f in findings if f.classification == "UNCLASSIFIED"]
    report = {
        "scanner": "pados.release.secret_scan",
        "rules": sorted(rule.value for rule in SecretRule),
        "scanned_files": scanned_files,
        "findings": [f.as_record() for f in findings],
        "unclassified": unclassified,
        "status": "PASS" if not unclassified else "FAIL",
        "values_recorded": False,
    }
    assert_no_credentials(report)
    return report
