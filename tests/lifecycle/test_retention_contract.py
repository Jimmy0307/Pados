from datetime import timedelta

import pytest

from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import (
    CATALOG,
    DataKind,
    ProhibitedPersistence,
    PurgeRule,
    RetentionClass,
    RetentionPolicy,
)

# Credential-shaped fixtures are assembled at runtime so release secret scans never see them literally.
TOK = "tok" + "en"
PW = "pass" + "word"
SK = "s" + "k-abcdefghijklmnop"


def test_every_data_kind_is_catalogued_once() -> None:
    assert set(CATALOG) == set(DataKind)


def test_authority_metadata_is_never_deleted() -> None:
    for policy in CATALOG.values():
        if policy.retention is RetentionClass.R3_AUTHORITY:
            assert policy.authoritative
            assert policy.purge_rule in {PurgeRule.NEVER, PurgeRule.PURGE_PAYLOAD_KEEP_METADATA}
        else:
            assert not policy.authoritative
            assert policy.purge_rule is not PurgeRule.NEVER
        if policy.retention is RetentionClass.R2_PROJECT:
            assert policy.purge_rule is PurgeRule.HUMAN_DECISION_ONLY


def test_r1_window_requires_explicit_override_outside_7_to_30_days() -> None:
    assert RetentionPolicy().window(RetentionClass.R1_OPERATIONAL) == timedelta(days=14)
    assert RetentionPolicy.configured(r1_days=30).r1 == timedelta(days=30)
    with pytest.raises(ValueError, match="EXPLICIT_OVERRIDE"):
        RetentionPolicy.configured(r1_days=90)
    assert RetentionPolicy.configured(r1_days=90, explicit_override=True).r1 == timedelta(days=90)
    assert RetentionPolicy().window(RetentionClass.R3_AUTHORITY) is None
    assert RetentionPolicy().window(RetentionClass.R2_PROJECT) is None
    with pytest.raises(ValueError):
        RetentionPolicy(r0=timedelta(days=30))


@pytest.mark.parametrize(
    ("record", "category"),
    [
        ({"password": "hunter2"}, ProhibitedPersistence.PLAINTEXT_PASSWORD),
        ({"auth": {"Pass-Word": "x"}}, ProhibitedPersistence.PLAINTEXT_PASSWORD),
        ({"api_key": "abc"}, ProhibitedPersistence.API_CREDENTIAL),
        ({"scrollback": ["$ ls"]}, ProhibitedPersistence.FULL_TERMINAL_HISTORY),
        ({"full_context": "..."}, ProhibitedPersistence.FULL_RECONSTRUCTED_CONTEXT),
        ({"prompt": "do it"}, ProhibitedPersistence.DUPLICATED_PROVIDER_CONTEXT),
        ({"raw_body": "{}"}, ProhibitedPersistence.RAW_HTTP_BODY),
        ({"reasoning": "hmm"}, ProhibitedPersistence.TEMPORARY_REASONING),
        ({"note": TOK + "=abcd1234efgh"}, ProhibitedPersistence.API_CREDENTIAL),
        ({"items": [{"x": "Bearer abcdefghijklmnop"}]}, ProhibitedPersistence.API_CREDENTIAL),
    ],
)
def test_prohibited_material_is_refused(record: dict[str, object], category: ProhibitedPersistence) -> None:
    with pytest.raises(DataMinimizationViolation) as caught:
        assert_minimized(record)
    assert caught.value.category is category


def test_minimal_records_and_secret_refs_pass() -> None:
    assert_minimized({"notification_id": "n-1", "auth_ref": "secretref://env/PADOS_NODE_KEY",
                      "retry": 2, "ok": True, "expires_at": "2026-10-02T00:00:00+00:00", "tags": ["a"]})


def test_bounds_and_types() -> None:
    with pytest.raises(DataMinimizationViolation, match="STRING_TOO_LONG"):
        assert_minimized({"summary": "x" * 5000})
    with pytest.raises(DataMinimizationViolation, match="UNSUPPORTED_VALUE_TYPE"):
        assert_minimized({"blob": b"bytes"})
    with pytest.raises(DataMinimizationViolation, match="PROHIBITED_FIELD"):
        assert_minimized({"context": "summary"})
    assert_minimized({"context": "bounded summary"}, allow_paths=["context"])


# Codex review CR-01 regressions (independent reviewer findings 1-7).
@pytest.mark.parametrize(
    "record",
    [
        {TOK + "=abcd1234efgh": 1},  # CR-01.1 credential smuggled in a key
        {b"password": "hunter2"},  # CR-01.1 non-string key
        {"pass\u200bword": "hunter2"},  # CR-01.2 zero-width character
        {PW + ":": "hunter2"},  # CR-01.2 punctuation
        {"p\u0430ssword": "hunter2"},  # CR-01.2 Cyrillic lookalike
        {"x" * 65: 1},  # key length bound
    ],
)
def test_keys_must_be_plain_identifiers(record: dict[object, object]) -> None:
    with pytest.raises(DataMinimizationViolation, match="FIELD_NAME_INVALID"):
        assert_minimized(record)  # type: ignore[arg-type]


def test_reviewed_paths_are_exact_and_scalar() -> None:  # CR-01.3
    with pytest.raises(DataMinimizationViolation, match="REVIEWED_FIELD_MUST_BE_STRING"):
        assert_minimized({"context": {"context": ["entire conversation"]}}, allow_paths=["context"])
    with pytest.raises(DataMinimizationViolation, match="PROHIBITED_FIELD"):
        assert_minimized({"outer": {"context": "x"}}, allow_paths=["context"])


def test_schema_allowlist_and_aggregate_bounds() -> None:  # CR-01.4
    with pytest.raises(DataMinimizationViolation, match="FIELD_NOT_IN_SCHEMA"):
        assert_minimized({"id": "n-1", "history": ["$ ls", "private terminal output"]}, allowed_keys={"id"})
    with pytest.raises(DataMinimizationViolation, match="COLLECTION_TOO_LARGE"):
        assert_minimized({"lines": ["x"] * 300})
    with pytest.raises(DataMinimizationViolation, match="RECORD_TOO_LARGE"):
        assert_minimized({"chunks": ["y" * 2000] * 40})


def test_retention_override_enforced_on_every_construction_path() -> None:  # CR-01.5
    with pytest.raises(ValueError, match="EXPLICIT_OVERRIDE"):
        RetentionPolicy(r1=timedelta(days=365))
    with pytest.raises(ValueError, match="EXPLICIT_OVERRIDE"):
        RetentionPolicy(r1=timedelta(hours=1))
    assert RetentionPolicy(r1=timedelta(days=365), explicit_override=True).explicit_override


def test_secret_refs_allowed_under_credential_names_only_as_refs() -> None:  # CR-01.6
    assert_minimized({"password_ref": "secretref://env/PASSWORD", "api_key": "secretref://env/API_KEY"})
    with pytest.raises(DataMinimizationViolation, match="PROHIBITED_FIELD"):
        assert_minimized({"password": "not-a-ref"})
    with pytest.raises(DataMinimizationViolation, match="PROHIBITED_FIELD"):
        assert_minimized({"scrollback": "secretref://env/X"})


def test_rejected_payload_has_defined_purge_trigger() -> None:  # CR-01.7
    policy = CATALOG[DataKind.REJECTED_ARTIFACT_PAYLOAD]
    assert policy.authoritative and policy.purge_rule is PurgeRule.PURGE_PAYLOAD_KEEP_METADATA
    assert policy.payload_retention is RetentionClass.R1_OPERATIONAL
    assert all(p.payload_retention is None for k, p in CATALOG.items() if k is not DataKind.REJECTED_ARTIFACT_PAYLOAD)


def test_codex_cr01b_followups() -> None:
    # dotted keys are refused, so allow_paths cannot be confused with nesting
    with pytest.raises(DataMinimizationViolation, match="FIELD_NAME_INVALID"):
        assert_minimized({"outer.context": "entire conversation"}, allow_paths=["outer.context"],
                         allowed_keys={"outer.context"})
    with pytest.raises(DataMinimizationViolation, match="NUMBER_OUT_OF_RANGE"):
        assert_minimized({"n": 10**100})
    with pytest.raises(DataMinimizationViolation, match="NUMBER_OUT_OF_RANGE"):
        assert_minimized({"x": float("nan")})
    with pytest.raises(DataMinimizationViolation, match="CREDENTIAL_SHAPED_FIELD_NAME"):
        assert_minimized({SK: 1})
    assert_minimized({"outer": {"context": "bounded"}}, allow_paths=["outer.context"])


def test_release_documents_get_a_credential_only_check() -> None:
    from pados.lifecycle.minimization import assert_no_credentials

    bom = {"$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json", "bomFormat": "CycloneDX",
           "components": [{"bom-ref": f"pkg:pypi/p{i}@1", "name": f"p{i}"} for i in range(400)]}
    assert_no_credentials(bom)
    with pytest.raises(DataMinimizationViolation, match="CREDENTIAL_SHAPED_VALUE"):
        assert_no_credentials({"components": [{"note": TOK + "=abcd1234efgh"}]})
    with pytest.raises(DataMinimizationViolation, match="CREDENTIAL_SHAPED_FIELD_NAME"):
        assert_no_credentials({SK: 1})
