"""Repository secret scanner: high-confidence rules, allowlist, values never recorded."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pados.release.secret_scan import (
    SecretRule,
    SecretScanError,
    load_allowlist,
    scan_files,
    scan_report,
    scan_text,
)
from tests.windows_links import create_link_or_skip

from .support import ROOT

# Token-shaped fixtures are assembled at runtime so this file never trips the scanner itself.
SAMPLES = {
    SecretRule.GITHUB_TOKEN: "ghp" + "_" + "A1b2C3d4" * 5,
    SecretRule.AWS_ACCESS_KEY_ID: "AKIA" + "Q7ZK3M5N2P4R6T8V",
    SecretRule.SLACK_TOKEN: "xoxb" + "-" + "1234567890-abcdefghij",
    SecretRule.LLM_PROVIDER_KEY: "sk" + "-ant-" + "x" * 40,
    SecretRule.GOOGLE_API_KEY: "AIza" + "S" * 35,
    SecretRule.STRIPE_LIVE_KEY: "sk" + "_live_" + "a1" * 12,
    SecretRule.PRIVATE_KEY_BLOCK: "-----BEGIN " + "RSA PRIVATE KEY-----",
    SecretRule.URL_EMBEDDED_PASSWORD: "postgresql://pados:" + "hunter22real" + "@db.example.com/pados",
}


@pytest.mark.parametrize("rule", list(SAMPLES))
def test_each_rule_detects_its_shape(rule: SecretRule) -> None:
    assert (2, rule) in scan_text("f", "header\nvalue = " + SAMPLES[rule] + "\n")


@pytest.mark.parametrize("text", [
    "postgresql://pados:***@127.0.0.1/pados",
    "postgresql://{user}:{secret}@host/db",
    "https://user:${PASSWORD}@example.com",
    "postgresql+psycopg://pados:<password>@localhost/pados",
    "sk-short",
    "tok" + "en = secretref://vault/pados/api",
])
def test_placeholders_are_not_findings(text: str) -> None:
    assert scan_text("f", text) == []


def test_report_never_contains_the_value(tmp_path: Path) -> None:
    value = SAMPLES[SecretRule.GITHUB_TOKEN]
    (tmp_path / "leak.txt").write_text(f"x\n{value}\n", encoding="utf-8")
    findings = scan_files(tmp_path, ["leak.txt"], ())
    report = scan_report(findings, scanned_files=1)
    assert report["status"] == "FAIL"
    assert report["unclassified"] == [{"path": "leak.txt", "line": 2, "rule": "GITHUB_TOKEN",
                                       "classification": "UNCLASSIFIED", "reason": ""}]
    assert value not in json.dumps(report) and report["values_recorded"] is False


def test_allowlist_classifies_with_reason(tmp_path: Path) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "fake.txt").write_text(SAMPLES[SecretRule.AWS_ACCESS_KEY_ID], encoding="utf-8")
    allowlist = load_allowlist([{"path": "fixtures/*", "rule": "AWS_ACCESS_KEY_ID", "reason": "documented fake"}])
    report = scan_report(scan_files(tmp_path, ["fixtures/fake.txt"], allowlist), scanned_files=1)
    assert report["status"] == "PASS" and report["findings"][0]["classification"] == "ALLOWED"
    with pytest.raises(SecretScanError):
        load_allowlist([{"path": "x", "rule": "AWS_ACCESS_KEY_ID"}])


def test_symlinks_and_binaries_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text(SAMPLES[SecretRule.SLACK_TOKEN], encoding="utf-8")
    create_link_or_skip(tmp_path / "real.txt", tmp_path / "link.txt", directory=False)
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe" + SAMPLES[SecretRule.SLACK_TOKEN].encode())
    assert scan_files(tmp_path, ["link.txt", "bin.dat"], ()) == []


def test_repository_scan_script_passes(tmp_path: Path) -> None:
    out = tmp_path / "secret-scan.json"
    done = subprocess.run([sys.executable, str(ROOT / "scripts/release/secret_scan.py"), str(out)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SECRET_SCAN_OK" in done.stdout
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "PASS"


def test_supply_chain_files_pass_the_release_packager_scan() -> None:
    from pados.release.package import _scan_secret

    roots = ["src/pados/release", "tests/release_supply_chain", "docs/supply_chain", "scripts/release"]
    files = [p for root in roots for p in (ROOT / root).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files.append(ROOT / "docs/product/CI_RELEASE_GATES.md")
    assert [str(p.relative_to(ROOT)) for p in files if _scan_secret(p)] == []


def test_supply_chain_files_have_no_unclassified_public_export_findings(tmp_path: Path) -> None:
    out = tmp_path / "public-export.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/release/public_export_check.py"), str(out)],
                   capture_output=True, text=True, check=False)
    mine = (".github/", "src/pados/release/", "tests/release_supply_chain/", "docs/supply_chain/",
            "scripts/release/", "docs/product/CI_RELEASE_GATES.md")
    unclassified = json.loads(out.read_text(encoding="utf-8"))["unclassified"]
    assert [f for f in unclassified if f["path"].startswith(mine)] == []


def test_url_password_rule_is_linear_on_long_lines() -> None:
    import time

    from pados.release.secret_scan import SecretRule, scan_text

    start = time.monotonic()
    assert scan_text("x", "a" * 300_000) == []
    assert time.monotonic() - start < 2.0
    hit = "conn = " + "postgres" + "://user:" + "s3cret" + "@db.internal/x"
    assert (1, SecretRule.URL_EMBEDDED_PASSWORD) in scan_text("x", hit)
